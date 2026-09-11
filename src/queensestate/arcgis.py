"""Async client for the ArcGIS REST layers and Hub search API behind data.charlottenc.gov.

The City of Charlotte Open Data Portal is an ArcGIS Hub site. Its datasets are ArcGIS REST
layers hosted on the city's GIS server, Mecklenburg County's GIS server, and ArcGIS Online.
This module wraps the small part of that API the MCP tools need.
"""

import html
import json
import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import anyio
import httpx

from queensestate.geo import Point

# Mecklenburg County's server rejects the default Python user agents with HTTP 403.
USER_AGENT: Final = "queensestate/0.1 (+https://data.charlottenc.gov)"
PORTAL_URL: Final = "https://data.charlottenc.gov"
HUB_SEARCH_URL: Final = f"{PORTAL_URL}/api/search/v1/collections/dataset/items"
# ArcGIS dates are UTC instants; residents think in local time (e.g. a report dated
# "Sept 6" is stored as 04:00Z), so output timestamps carry Charlotte's UTC offset.
CHARLOTTE_TZ: Final = ZoneInfo("America/New_York")

# Hosts serving the layers listed in the portal catalog. Everything else is refused so the
# generic query tools cannot be pointed at arbitrary URLs.
ALLOWED_HOSTS: Final = frozenset(
    {
        "gis.charlottenc.gov",
        "gis.ci.charlotte.nc.us",
        "meckgis.mecklenburgcountync.gov",
        "services.arcgis.com",
    }
)

_LAYER_PATH: Final = re.compile(
    r"(?:/[\w.-]+)*/rest/services(?:/[\w.-]+)+/(?:MapServer|FeatureServer)/\d+", re.IGNORECASE
)
_RETRYABLE_STATUS: Final = frozenset({429, 500, 502, 503, 504})
_BOOKKEEPING_FIELD: Final = re.compile(
    r"objectid(?:_1)?|globalid|gdb_geomattr_data|created_user|last_edited_user|"
    r"shape(?:__area|__length|\.starea\(\)|\.stlength\(\))?",
    re.IGNORECASE,
)
_HTML_TAG: Final = re.compile(r"<[^>]+>")

type StatisticType = Literal["count", "sum", "min", "max", "avg", "stddev"]


class ArcGISError(Exception):
    """An ArcGIS service or the Hub API failed or returned an unusable payload."""


@dataclass(frozen=True, slots=True)
class Feature:
    """One record from a layer query, with date fields rendered as ISO 8601 strings."""

    attributes: dict[str, Any]
    geometry: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Records returned by a layer query.

    ``exceeded_transfer_limit`` is True when more records matched than were returned.
    """

    features: list[Feature]
    exceeded_transfer_limit: bool


def validate_layer_url(url: str) -> str:
    """Normalize ``url`` and check that it names one layer on an allowlisted ArcGIS host.

    Raises:
        ValueError: If ``url`` is not an https ``.../MapServer/<n>`` or
            ``.../FeatureServer/<n>`` URL on one of :data:`ALLOWED_HOSTS`.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    if (
        parts.scheme != "https"
        or parts.hostname not in ALLOWED_HOSTS
        or parts.port not in (None, 443)
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or ".." in path
        or not _LAYER_PATH.fullmatch(path)
    ):
        raise ValueError(
            "Expected an https ArcGIS layer URL ending in /MapServer/<n> or /FeatureServer/<n> "
            f"on one of: {', '.join(sorted(ALLOWED_HOSTS))}."
        )
    return f"https://{parts.hostname}{path}"


def sql_quote(value: str) -> str:
    """Return ``value`` as a single-quoted SQL string literal for an ArcGIS where clause."""
    return "'" + value.replace("'", "''") + "'"


def sql_timestamp(moment: datetime) -> str:
    """Return an ArcGIS standardized-SQL timestamp literal for ``moment`` in UTC."""
    return f"TIMESTAMP '{moment.astimezone(UTC):%Y-%m-%d %H:%M:%S}'"


def statistic(kind: StatisticType, field: str, alias: str) -> dict[str, str]:
    """Build one ``outStatistics`` entry."""
    return {"statisticType": kind, "onStatisticField": field, "outStatisticFieldName": alias}


def epoch_ms_to_iso(value: object) -> object:
    """Convert ArcGIS epoch milliseconds to ISO 8601 in Charlotte local time.

    Values that are not numbers, or are out of range, are returned unchanged.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return value
    try:
        moment = datetime.fromtimestamp(value / 1000, tz=CHARLOTTE_TZ)
        return moment.isoformat(timespec="seconds")
    except OverflowError, OSError, ValueError:
        return value


def clean_attributes(attributes: Mapping[str, Any], date_fields: frozenset[str]) -> dict[str, Any]:
    """Drop bookkeeping and empty fields, trim strings, and render date fields as ISO 8601."""
    cleaned: dict[str, Any] = {}
    for name, value in attributes.items():
        if value is None or _BOOKKEEPING_FIELD.fullmatch(name):
            continue
        if isinstance(value, str):
            text = value.strip()
            if text:
                cleaned[name] = text
        elif name in date_fields:
            cleaned[name] = epoch_ms_to_iso(value)
        else:
            cleaned[name] = value
    return cleaned


def html_to_text(markup: str, max_length: int = 1200) -> str:
    """Reduce an HTML description from the portal to plain text of bounded length."""
    text = " ".join(html.unescape(_HTML_TAG.sub(" ", markup)).split())
    return text if len(text) <= max_length else text[: max_length - 1].rstrip() + "…"


class ArcGISClient:
    """Thin async wrapper over ArcGIS REST ``query`` endpoints and the Hub search API.

    The caller owns ``http`` and must close it. Concurrent requests are bounded, and
    transient failures are retried with jittered exponential backoff; every request is an
    idempotent GET, so retrying is safe.
    """

    def __init__(
        self, http: httpx.AsyncClient, *, max_retries: int = 2, max_concurrency: int = 6
    ) -> None:
        self._http = http
        self._max_retries = max_retries
        self._limiter = anyio.CapacityLimiter(max_concurrency)

    async def get_json(self, url: str, params: Mapping[str, str]) -> dict[str, Any]:
        """GET ``url`` and return its JSON object, translating failures to ArcGISError."""
        for attempt in range(self._max_retries + 1):
            final_attempt = attempt == self._max_retries
            try:
                async with self._limiter:
                    response = await self._http.get(url, params=params)
            except httpx.TransportError as exc:
                if final_attempt:
                    raise ArcGISError(
                        f"Could not reach {_host(url)} ({type(exc).__name__}). Try again shortly."
                    ) from exc
            else:
                if final_attempt or response.status_code not in _RETRYABLE_STATUS:
                    return _decode(response)
            await anyio.sleep(_backoff_seconds(attempt))
        raise AssertionError("unreachable: the final attempt returns or raises")

    async def layer_metadata(self, layer_url: str) -> dict[str, Any]:
        """Return a layer's service metadata (name, fields, geometry type, capabilities)."""
        return await self.get_json(layer_url, {"f": "json"})

    async def query(
        self,
        layer_url: str,
        *,
        where: str = "1=1",
        out_fields: Sequence[str] = ("*",),
        near: Point | None = None,
        radius_miles: float | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        return_geometry: bool = False,
    ) -> QueryResult:
        """Query one layer.

        With ``near``, only features intersecting that point are returned, or with
        ``radius_miles`` those within that distance of it. Geometry, when requested, is in
        WGS84 longitude/latitude and generalized to about five meters.
        """
        params = _filter_params(where, near, radius_miles)
        params |= {"outFields": ",".join(out_fields), "outSR": "4326"}
        if return_geometry:
            params |= {
                "returnGeometry": "true",
                "geometryPrecision": "6",
                "maxAllowableOffset": "0.00005",
            }
        else:
            params["returnGeometry"] = "false"
        if order_by:
            params["orderByFields"] = order_by
        if limit is not None:
            params["resultRecordCount"] = str(limit)
        payload = await self.get_json(f"{layer_url}/query", params)
        date_fields = _date_fields(payload)
        features = [
            Feature(
                attributes=clean_attributes(raw.get("attributes") or {}, date_fields),
                geometry=raw.get("geometry") if return_geometry else None,
            )
            for raw in payload.get("features") or ()
        ]
        return QueryResult(
            features=features,
            exceeded_transfer_limit=bool(payload.get("exceededTransferLimit")),
        )

    async def count(
        self,
        layer_url: str,
        *,
        where: str = "1=1",
        near: Point | None = None,
        radius_miles: float | None = None,
    ) -> int:
        """Return how many features match the filter."""
        params = _filter_params(where, near, radius_miles) | {"returnCountOnly": "true"}
        payload = await self.get_json(f"{layer_url}/query", params)
        count = payload.get("count")
        if not isinstance(count, int):
            raise ArcGISError(f"{_host(layer_url)} did not return a record count")
        return count

    async def statistics(
        self,
        layer_url: str,
        *,
        statistics: Sequence[Mapping[str, str]],
        group_by: Sequence[str] = (),
        where: str = "1=1",
        near: Point | None = None,
        radius_miles: float | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Compute summary statistics server-side, optionally grouped by fields."""
        params = _filter_params(where, near, radius_miles)
        params["outStatistics"] = json.dumps([dict(entry) for entry in statistics])
        if group_by:
            params["groupByFieldsForStatistics"] = ",".join(group_by)
        if order_by:
            params["orderByFields"] = order_by
        if limit is not None:
            params["resultRecordCount"] = str(limit)
        payload = await self.get_json(f"{layer_url}/query", params)
        date_fields = _date_fields(payload)
        rows = [
            clean_attributes(raw.get("attributes") or {}, date_fields)
            for raw in payload.get("features") or ()
        ]
        # Some map services ignore resultRecordCount for statistics queries.
        return rows if limit is None else rows[:limit]

    async def search_catalog(
        self, text: str, limit: int
    ) -> tuple[int | None, list[dict[str, Any]]]:
        """Full-text search of the portal's dataset catalog.

        Returns the total number of matches (when reported) and the matching records.
        """
        payload = await self.get_json(HUB_SEARCH_URL, {"q": text, "limit": str(limit)})
        records = payload.get("features")
        if not isinstance(records, list):
            raise ArcGISError("The portal search API returned an unexpected payload")
        matched = payload.get("numberMatched")
        return (
            matched if isinstance(matched, int) else None,
            [record for record in records if isinstance(record, dict)],
        )


def _filter_params(where: str, near: Point | None, radius_miles: float | None) -> dict[str, str]:
    params = {"f": "json", "where": where}
    if near is not None:
        params |= {
            "geometry": f"{near.longitude},{near.latitude}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
        }
        if radius_miles is not None:
            params |= {"distance": f"{radius_miles:g}", "units": "esriSRUnit_StatuteMile"}
    return params


def _date_fields(payload: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(
        field["name"]
        for field in payload.get("fields") or ()
        if isinstance(field, dict) and field.get("type") == "esriFieldTypeDate"
    )


def _decode(response: httpx.Response) -> dict[str, Any]:
    host = response.request.url.host
    if response.is_error:
        raise ArcGISError(f"{host} returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ArcGISError(f"{host} returned a response that is not JSON") from exc
    if not isinstance(payload, dict):
        raise ArcGISError(f"{host} returned unexpected JSON")
    error = payload.get("error")
    if isinstance(error, dict):
        details = "; ".join(str(detail) for detail in error.get("details") or () if detail)
        message = f"{host} rejected the request: {error.get('message') or 'unknown error'}"
        raise ArcGISError(f"{message} ({details})" if details else message)
    return payload


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


def _backoff_seconds(attempt: int) -> float:
    # Jitter spreads out retries from concurrent tool calls; it is not security-sensitive.
    return min(4.0, 0.5 * 2.0**attempt) * random.uniform(0.5, 1.0)  # noqa: S311
