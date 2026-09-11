"""MCP server that lets LLM clients answer residents' questions with Charlotte open data.

Data comes from the City of Charlotte Open Data Portal (https://data.charlottenc.gov), an
ArcGIS Hub site whose datasets are ArcGIS REST layers run by the City of Charlotte and
Mecklenburg County. Every tool is read-only.
"""

import argparse
import asyncio
import logging
import math
import re
from collections import Counter
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from queensestate import __version__, catalog
from queensestate.address import ResolvedLocation, geocode, resolve_location
from queensestate.arcgis import (
    CHARLOTTE_TZ,
    PORTAL_URL,
    USER_AGENT,
    ArcGISClient,
    ArcGISError,
    Feature,
    StatisticType,
    epoch_ms_to_iso,
    html_to_text,
    sql_quote,
    sql_timestamp,
    statistic,
    validate_layer_url,
)
from queensestate.geo import Point, distance_to_geometry_miles

INSTRUCTIONS: Final = """\
Public data from the City of Charlotte, NC Open Data Portal (data.charlottenc.gov), published
by the City of Charlotte and Mecklenburg County.

- Location tools accept a Mecklenburg County street address or 'latitude,longitude'.
- Start with get_address_profile for "what serves my address" questions: city council
  member, county commissioner, police division, fire station, trash/recycling days, zoning,
  flood zone, and historic district.
- For anything the purpose-built tools don't cover, use search_datasets, then
  describe_dataset, then query_dataset or summarize_dataset.
- Timestamps are ISO 8601 in Charlotte local time (America/New_York) with the UTC offset.
- The data can lag real-world events and is not an emergency service: for emergencies call
  911; for city services call 311 or use the CLT+ app.
"""

_READ_ONLY: Final = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
_DAY_NAMES: Final = {
    "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
    "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday",
}  # fmt: skip
_FIELD_NAME: Final = re.compile(r"[A-Za-z_][\w.]*")
_ORDER_BY: Final = re.compile(
    r"[A-Za-z_][\w.]*(?:\s+(?:ASC|DESC))?(?:\s*,\s*[A-Za-z_][\w.]*(?:\s+(?:ASC|DESC))?)*",
    re.IGNORECASE,
)
_SEARCH_TEXT_JUNK: Final = re.compile(r"[^A-Z0-9 /&-]+")
_COUNT_ALIAS: Final = "record_count"

LocationText = Annotated[
    str,
    Field(
        min_length=3,
        max_length=200,
        description=(
            "A street address in Mecklenburg County, e.g. '600 E 4th St, Charlotte, NC 28202', "
            "or coordinates as 'latitude,longitude', e.g. '35.2271,-80.8431'."
        ),
    ),
]
OptionalLocationText = Annotated[
    str | None,
    Field(
        min_length=3,
        max_length=200,
        description=(
            "Optional street address or 'latitude,longitude'; when given, only records within "
            "radius_miles of it are returned."
        ),
    ),
]
LayerUrl = Annotated[
    str,
    Field(
        max_length=300,
        description=(
            "ArcGIS layer URL from search_datasets, ending in /MapServer/<n> or /FeatureServer/<n>."
        ),
    ),
]
Limit = Annotated[int, Field(ge=1, le=100, description="Maximum number of records to return.")]


# --------------------------------------------------------------------------------------
# Result models
# --------------------------------------------------------------------------------------


class LocationInfo(BaseModel):
    """The point a tool searched around."""

    query: str
    matched_address: str | None = Field(
        default=None, description="County master-address match; None when coordinates were given."
    )
    latitude: float
    longitude: float


class CountByValue(BaseModel):
    value: str
    count: int


class RecordsResult(BaseModel):
    """Records matching a filter, optionally near a location."""

    location: LocationInfo | None = None
    radius_miles: float | None = None
    since: str | None = Field(default=None, description="Earliest date included (local time).")
    total_matching: int = Field(description="Records matching the filters; may exceed returned.")
    breakdown_by: str | None = None
    breakdown: list[CountByValue] = Field(
        default_factory=list, description="Counts of all matching records per value."
    )
    records: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)


class SummaryResult(BaseModel):
    """Server-side aggregate of a dataset."""

    source: str
    filters: str = Field(description="The SQL where clause that was applied.")
    group_by: list[str]
    statistic: str
    location: LocationInfo | None = None
    radius_miles: float | None = None
    rows: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)


class DatasetSummary(BaseModel):
    id: str
    title: str
    type: str | None
    summary: str | None
    layer_url: str | None = Field(
        description="Pass to describe_dataset, query_dataset, or summarize_dataset. None if the "
        "item is not a single queryable layer."
    )
    portal_page: str
    last_modified: str | None
    tags: list[str]


class DatasetSearchResult(BaseModel):
    query: str
    total_matching: int | None
    datasets: list[DatasetSummary]


class FieldInfo(BaseModel):
    name: str
    type: str
    alias: str | None


class DatasetDescription(BaseModel):
    layer_url: str
    name: str
    description: str | None
    geometry_type: str | None
    record_count: int
    max_records_per_query: int | None
    supports_statistics: bool
    fields: list[FieldInfo]


class AddressCandidate(BaseModel):
    full_address: str
    latitude: float
    longitude: float
    tax_parcel_id: str | None
    jurisdiction: str | None = Field(description="Municipality, e.g. CHARLOTTE or MATTHEWS.")
    postal_city: str | None
    zip_code: str | None


class AddressLookupResult(BaseModel):
    query: str
    candidates: list[AddressCandidate]
    note: str | None = None


class CouncilDistrict(BaseModel):
    district: str
    representative: str | None
    email: str | None


class CommissionerDistrict(BaseModel):
    district: str
    commissioner: str | None


class FireStation(BaseModel):
    station_number: int
    battalion: int | None
    name: str | None
    address: str | None


class Zoning(BaseModel):
    district: str = Field(description="Zoning district code, e.g. 'N1-A'.")
    description: str | None
    overlay: str | None
    petition: str | None = Field(description="Rezoning petition that set this zoning, if any.")


class CollectionDay(BaseModel):
    day: str
    served_by: str | None
    note: str | None


class CollectionSchedule(BaseModel):
    garbage: CollectionDay | None
    recycling: CollectionDay | None = Field(
        description="Every other week; the note says whether it is the GREEN or ORANGE week."
    )
    yard_waste: CollectionDay | None


class AddressProfile(BaseModel):
    """Public services, representatives, and regulations that apply at a location."""

    location: LocationInfo
    jurisdiction: str | None
    tax_parcel_id: str | None
    zip_code: str | None
    city_council: CouncilDistrict | None = Field(
        description="None when the point is outside Charlotte's council districts."
    )
    county_commissioner: CommissionerDistrict | None
    police_division: str | None
    fire_station: FireStation | None = Field(description="First-due Charlotte Fire station.")
    trash_and_recycling: CollectionSchedule | None = Field(
        description="City of Charlotte residential collection; None if not on a city route."
    )
    zoning: Zoning | None
    fema_flood_zone: str | None = Field(
        description="FEMA flood zone if inside a mapped FEMA floodplain, otherwise None."
    )
    historic_district: str | None
    unavailable: list[str] = Field(
        description="Sections that could not be retrieved because a source service failed."
    )


class TrashScheduleResult(BaseModel):
    location: LocationInfo
    schedule: CollectionSchedule | None
    notes: list[str]


class NearbyPlacesResult(BaseModel):
    location: LocationInfo
    category: str
    radius_miles: float
    total_within_radius: int
    places: list[dict[str, Any]]
    notes: list[str]


class BudgetRow(BaseModel):
    name: str
    amount: float


class BudgetResult(BaseModel):
    fiscal_year: str
    available_fiscal_years: list[str]
    group_by: str
    department_filter: str | None
    net_total_amount: float = Field(description="Sum of all lines, including negative ones.")
    positive_total_amount: float = Field(description="Sum of lines with positive amounts.")
    rows: list[BudgetRow]
    notes: list[str]


class SalaryRow(BaseModel):
    group: str
    employees: int
    average_annual_rate: float | None
    minimum_annual_rate: float | None
    maximum_annual_rate: float | None


class SalaryResult(BaseModel):
    year: int
    quarter: int
    group_by: str
    filters: str
    overall: SalaryRow
    rows: list[SalaryRow]
    notes: list[str]


# --------------------------------------------------------------------------------------
# Server plumbing
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppState:
    client: ArcGISClient


@asynccontextmanager
async def _lifespan(_server: MCPServer[AppState]) -> AsyncIterator[AppState]:
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=httpx.Timeout(30.0, connect=10.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    ) as http:
        yield AppState(client=ArcGISClient(http))


def _client(ctx: Context[AppState, Any]) -> ArcGISClient:
    return ctx.request_context.lifespan_context.client


@contextmanager
def _user_facing_errors() -> Iterator[None]:
    """Report expected failures to the model as tool errors with a useful message.

    Invalid input (ValueError, including LocationError) and upstream ArcGIS failures become
    ToolError. Anything else is left to the SDK, which reports a generic internal error.
    """
    try:
        yield
    except (ArcGISError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@asynccontextmanager
async def _task_group() -> AsyncIterator[asyncio.TaskGroup]:
    """A TaskGroup that re-raises its first failure directly rather than as an ExceptionGroup."""
    try:
        async with asyncio.TaskGroup() as group:
            yield group
    except ExceptionGroup as failure:
        raise failure.exceptions[0] from failure


async def _optional[T](
    name: str, operation: Coroutine[Any, Any, T], unavailable: list[str]
) -> T | None:
    """Await ``operation``; on an upstream failure record ``name`` and return None."""
    try:
        return await operation
    except ArcGISError:
        unavailable.append(name)
        return None


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(float(value), 2)


def _iso(moment: datetime) -> str:
    return moment.astimezone(CHARLOTTE_TZ).isoformat(timespec="seconds")


def _location_info(location: ResolvedLocation) -> LocationInfo:
    return LocationInfo(
        query=location.query,
        matched_address=location.address.full_address if location.address else None,
        latitude=round(location.point.latitude, 6),
        longitude=round(location.point.longitude, 6),
    )


def _recent(field: str, days: int) -> tuple[str, datetime]:
    """Where clause for ``field`` within the last ``days`` days, and the window start."""
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    # The upper bound excludes mistyped future dates found in some city datasets.
    until = now + timedelta(days=1)
    return f"{field} >= {sql_timestamp(since)} AND {field} <= {sql_timestamp(until)}", since


def _contains(field: str, text: str) -> str:
    """Case-insensitive substring filter on ``field``, restricted to safe characters."""
    cleaned = " ".join(_SEARCH_TEXT_JUNK.sub(" ", text.upper()).split())
    if not cleaned:
        raise ValueError(f"Search text {text!r} must contain letters or digits.")
    return f"UPPER({field}) LIKE {sql_quote(f'%{cleaned}%')}"


def _validated_fields(names: Sequence[str]) -> list[str]:
    for name in names:
        if not _FIELD_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a valid field name.")
    return list(names)


def _validated_order_by(order_by: str | None) -> str | None:
    if order_by is not None and not _ORDER_BY.fullmatch(order_by.strip()):
        raise ValueError("order_by must look like 'FIELD [ASC|DESC], FIELD2 [ASC|DESC]'.")
    return order_by.strip() if order_by else None


def _distance_key(record: dict[str, Any]) -> float:
    distance = record.get("distance_miles")
    return float(distance) if isinstance(distance, int | float) else math.inf


def _with_distance(feature: Feature, point: Point | None) -> dict[str, Any]:
    record = dict(feature.attributes)
    if point is not None:
        distance = distance_to_geometry_miles(point, feature.geometry)
        if distance is not None:
            record["distance_miles"] = round(distance, 2)
    return record


async def _records_near(
    client: ArcGISClient,
    layer_url: str,
    *,
    where: str,
    fields: Sequence[str],
    limit: int,
    order_by: str | None = None,
    location: ResolvedLocation | None = None,
    radius_miles: float | None = None,
    breakdown_field: str | None = None,
    since: datetime | None = None,
    notes: Sequence[str] = (),
) -> RecordsResult:
    """Fetch matching records together with a total count and an optional per-value breakdown."""
    point = location.point if location else None
    radius = radius_miles if point else None
    async with _task_group() as group:
        total = group.create_task(
            client.count(layer_url, where=where, near=point, radius_miles=radius)
        )
        records = group.create_task(
            client.query(
                layer_url,
                where=where,
                out_fields=fields,
                near=point,
                radius_miles=radius,
                order_by=order_by,
                limit=limit,
                return_geometry=point is not None,
            )
        )
        breakdown = (
            group.create_task(
                client.statistics(
                    layer_url,
                    statistics=[statistic("count", "OBJECTID", _COUNT_ALIAS)],
                    group_by=[breakdown_field],
                    where=where,
                    near=point,
                    radius_miles=radius,
                    order_by=f"{_COUNT_ALIAS} DESC",
                    limit=50,
                )
            )
            if breakdown_field
            else None
        )
    return RecordsResult(
        location=_location_info(location) if location else None,
        radius_miles=radius,
        since=_iso(since) if since else None,
        total_matching=total.result(),
        breakdown_by=breakdown_field,
        breakdown=[
            CountByValue(
                value=str(row.get(breakdown_field or "", "(blank)")),
                count=int(row.get(_COUNT_ALIAS, 0)),
            )
            for row in (breakdown.result() if breakdown else [])
        ],
        records=[_with_distance(feature, point) for feature in records.result().features],
        notes=list(notes),
    )


# --------------------------------------------------------------------------------------
# Tools: discovery and generic access
# --------------------------------------------------------------------------------------


async def search_datasets(
    ctx: Context[AppState, Any],
    query: Annotated[
        str,
        Field(
            min_length=2,
            max_length=200,
            description="Keywords, e.g. 'sidewalks', 'tree canopy', 'speed humps', 'census'.",
        ),
    ],
    limit: Annotated[int, Field(ge=1, le=25, description="Maximum datasets to return.")] = 10,
) -> DatasetSearchResult:
    """Search the Charlotte Open Data Portal catalog (about 380 datasets) by keyword.

    Use this to find data the purpose-built tools don't cover. Pass a result's layer_url to
    describe_dataset, query_dataset, or summarize_dataset.
    """
    with _user_facing_errors():
        total, records = await _client(ctx).search_catalog(query, limit)
    return DatasetSearchResult(
        query=query, total_matching=total, datasets=[_dataset_summary(r) for r in records]
    )


def _dataset_summary(record: dict[str, Any]) -> DatasetSummary:
    properties = record.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    dataset_id = str(record.get("id") or properties.get("id") or "")
    url = properties.get("url")
    try:
        layer_url = validate_layer_url(url) if isinstance(url, str) else None
    except ValueError:
        layer_url = None
    modified = epoch_ms_to_iso(properties.get("modified"))
    return DatasetSummary(
        id=dataset_id,
        title=str(properties.get("title") or "(untitled)"),
        type=_opt_str(properties.get("type")),
        summary=_opt_str(properties.get("snippet")),
        layer_url=layer_url,
        portal_page=f"{PORTAL_URL}/datasets/{dataset_id}",
        last_modified=modified if isinstance(modified, str) else None,
        tags=[str(tag) for tag in properties.get("tags") or ()][:10],
    )


async def describe_dataset(ctx: Context[AppState, Any], layer_url: LayerUrl) -> DatasetDescription:
    """Show a dataset layer's description, record count, and fields.

    Call this before query_dataset or summarize_dataset to learn field names and types.
    """
    client = _client(ctx)
    with _user_facing_errors():
        url = validate_layer_url(layer_url)
        async with _task_group() as group:
            metadata_task = group.create_task(client.layer_metadata(url))
            count_task = group.create_task(client.count(url))
    metadata = metadata_task.result()
    advanced = metadata.get("advancedQueryCapabilities")
    geometry_type = _opt_str(metadata.get("geometryType"))
    return DatasetDescription(
        layer_url=url,
        name=str(metadata.get("name") or ""),
        description=html_to_text(str(metadata.get("description") or "")) or None,
        geometry_type=geometry_type.removeprefix("esriGeometry") if geometry_type else None,
        record_count=count_task.result(),
        max_records_per_query=_opt_int(metadata.get("maxRecordCount")),
        supports_statistics=isinstance(advanced, dict) and bool(advanced.get("supportsStatistics")),
        fields=[
            FieldInfo(
                name=str(field["name"]),
                type=str(field.get("type", "")).removeprefix("esriFieldType"),
                alias=_opt_str(field.get("alias")),
            )
            for field in metadata.get("fields") or ()
            if isinstance(field, dict) and field.get("name")
        ],
    )


async def query_dataset(
    ctx: Context[AppState, Any],
    layer_url: LayerUrl,
    where: Annotated[
        str,
        Field(
            min_length=1,
            max_length=2000,
            description=(
                "SQL where clause over the layer's fields (see describe_dataset), e.g. "
                "\"ZIP = '28205'\" or \"DATE_REPORTED >= TIMESTAMP '2026-01-01 00:00:00'\". "
                "Use '1=1' for everything."
            ),
        ),
    ] = "1=1",
    fields: Annotated[
        list[str] | None, Field(max_length=40, description="Fields to return; omit for all.")
    ] = None,
    order_by: Annotated[
        str | None, Field(max_length=200, description="e.g. 'DATE_REPORTED DESC'.")
    ] = None,
    near: OptionalLocationText = None,
    radius_miles: Annotated[float, Field(gt=0, le=10, description="Used with near.")] = 0.5,
    limit: Limit = 25,
) -> RecordsResult:
    """Fetch records from any portal dataset layer with a SQL filter, optionally near a place."""
    client = _client(ctx)
    with _user_facing_errors():
        url = validate_layer_url(layer_url)
        out_fields = _validated_fields(fields) if fields else ["*"]
        ordering = _validated_order_by(order_by)
        location = await resolve_location(client, near) if near else None
        return await _records_near(
            client,
            url,
            where=where,
            fields=out_fields,
            order_by=ordering,
            limit=limit,
            location=location,
            radius_miles=radius_miles,
        )


async def summarize_dataset(
    ctx: Context[AppState, Any],
    layer_url: LayerUrl,
    group_by: Annotated[
        list[str], Field(min_length=1, max_length=3, description="Fields to group by.")
    ],
    where: Annotated[str, Field(min_length=1, max_length=2000)] = "1=1",
    statistic_type: StatisticType = "count",
    statistic_field: Annotated[
        str | None,
        Field(max_length=100, description="Numeric field to aggregate; required unless counting."),
    ] = None,
    near: OptionalLocationText = None,
    radius_miles: Annotated[float, Field(gt=0, le=10, description="Used with near.")] = 0.5,
    limit: Annotated[int, Field(ge=1, le=500, description="Maximum groups to return.")] = 50,
) -> SummaryResult:
    """Count or aggregate any portal dataset server-side, grouped by up to three fields.

    Answers questions like "how many 311 requests of each type were made in ZIP 28205?".
    Rows are sorted by the statistic, largest first, in a column named stat_value.
    """
    client = _client(ctx)
    with _user_facing_errors():
        url = validate_layer_url(layer_url)
        groups = _validated_fields(group_by)
        if statistic_type != "count" and not statistic_field:
            raise ValueError("statistic_field is required unless statistic_type is 'count'.")
        target = _validated_fields([statistic_field or groups[0]])[0]
        location = await resolve_location(client, near) if near else None
        rows = await client.statistics(
            url,
            statistics=[statistic(statistic_type, target, "stat_value")],
            group_by=groups,
            where=where,
            near=location.point if location else None,
            radius_miles=radius_miles if location else None,
            order_by="stat_value DESC",
            limit=limit,
        )
    notes = ["Counts include only records where the counted field is not null."]
    return SummaryResult(
        source=url,
        filters=where,
        group_by=groups,
        statistic=f"{statistic_type}({target})",
        location=_location_info(location) if location else None,
        radius_miles=radius_miles if location else None,
        rows=rows,
        notes=notes if statistic_type == "count" else [],
    )


# --------------------------------------------------------------------------------------
# Tools: my address
# --------------------------------------------------------------------------------------


async def lookup_address(
    ctx: Context[AppState, Any],
    address: Annotated[
        str,
        Field(min_length=3, max_length=200, description="e.g. '600 E 4th St, Charlotte'."),
    ],
    max_results: Annotated[int, Field(ge=1, le=10)] = 5,
) -> AddressLookupResult:
    """Find an address in Mecklenburg County's official master address list.

    Returns matches with coordinates, tax parcel ID, municipality, and ZIP code.
    """
    with _user_facing_errors():
        matches = await geocode(_client(ctx), address, max_matches=max_results)
    return AddressLookupResult(
        query=address,
        candidates=[
            AddressCandidate(
                full_address=match.full_address,
                latitude=round(match.point.latitude, 6),
                longitude=round(match.point.longitude, 6),
                tax_parcel_id=match.parcel_id,
                jurisdiction=match.jurisdiction,
                postal_city=match.postal_city,
                zip_code=match.zip_code,
            )
            for match in matches
        ],
        note=None if matches else "No match. Check the house number and street name spelling.",
    )


async def get_address_profile(
    ctx: Context[AppState, Any], location: LocationText
) -> AddressProfile:
    """Everything that serves or governs a location, in one call.

    Returns city council district and representative, county commissioner, CMPD patrol
    division, first-due fire station, trash/recycling/yard-waste days, zoning, FEMA flood
    zone, historic district, ZIP code, and tax parcel ID.
    """
    client = _client(ctx)
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
    point = resolved.point
    unavailable: list[str] = []
    async with asyncio.TaskGroup() as group:

        def start[T](name: str, operation: Coroutine[Any, Any, T]) -> asyncio.Task[T | None]:
            return group.create_task(_optional(name, operation, unavailable))

        council = start("city_council", _council_district(client, point))
        commissioner = start("county_commissioner", _commissioner_district(client, point))
        police = start("police_division", _police_division(client, point))
        fire = start("fire_station", _fire_station(client, point))
        trash = start("trash_and_recycling", _collection_schedule(client, point))
        zoning = start("zoning", _zoning(client, point))
        flood = start("fema_flood_zone", _flood_zone(client, point))
        historic = start("historic_district", _historic_district(client, point))
        zip_code = (
            None
            if resolved.address and resolved.address.zip_code
            else start("zip_code", _zip_code(client, point))
        )
    address = resolved.address
    return AddressProfile(
        location=_location_info(resolved),
        jurisdiction=address.jurisdiction if address else None,
        tax_parcel_id=address.parcel_id if address else None,
        zip_code=address.zip_code if address and address.zip_code else _task_value(zip_code),
        city_council=council.result(),
        county_commissioner=commissioner.result(),
        police_division=police.result(),
        fire_station=fire.result(),
        trash_and_recycling=trash.result(),
        zoning=zoning.result(),
        fema_flood_zone=flood.result(),
        historic_district=historic.result(),
        unavailable=sorted(unavailable),
    )


def _task_value[T](task: asyncio.Task[T | None] | None) -> T | None:
    return task.result() if task else None


async def _attributes_at(
    client: ArcGISClient, layer_url: str, point: Point, fields: Sequence[str]
) -> dict[str, Any] | None:
    result = await client.query(layer_url, near=point, out_fields=fields, limit=1)
    return result.features[0].attributes if result.features else None


async def _council_district(client: ArcGISClient, point: Point) -> CouncilDistrict | None:
    fields = ("District", "DistrictRep", "RepEmail")
    attributes = await _attributes_at(client, catalog.COUNCIL_DISTRICTS, point, fields)
    if not attributes or "District" not in attributes:
        return None
    return CouncilDistrict(
        district=str(attributes["District"]),
        representative=_opt_str(attributes.get("DistrictRep")),
        email=_opt_str(attributes.get("RepEmail")),
    )


async def _commissioner_district(client: ArcGISClient, point: Point) -> CommissionerDistrict | None:
    fields = ("longname", "cc_name")
    attributes = await _attributes_at(client, catalog.COMMISSIONER_DISTRICTS, point, fields)
    if not attributes or "longname" not in attributes:
        return None
    return CommissionerDistrict(
        district=str(attributes["longname"]), commissioner=_opt_str(attributes.get("cc_name"))
    )


async def _police_division(client: ArcGISClient, point: Point) -> str | None:
    attributes = await _attributes_at(client, catalog.POLICE_DIVISIONS, point, ("DNAME",))
    return _opt_str(attributes.get("DNAME")) if attributes else None


async def _fire_station(client: ArcGISClient, point: Point) -> FireStation | None:
    fields = ("Station", "Battalion")
    area = await _attributes_at(client, catalog.FIRE_STATION_AREAS, point, fields)
    number = _opt_int(area.get("Station")) if area else None
    if area is None or number is None:
        return None
    stations = await client.query(
        catalog.FIRE_STATIONS, where=f"NUM = {number}", out_fields=("NAME", "ADDRESS"), limit=1
    )
    details = stations.features[0].attributes if stations.features else {}
    return FireStation(
        station_number=number,
        battalion=_opt_int(area.get("Battalion")),
        name=_opt_str(details.get("NAME")),
        address=_opt_str(details.get("ADDRESS")),
    )


async def _zoning(client: ArcGISClient, point: Point) -> Zoning | None:
    fields = ("ZoneDes", "ZoneClass", "Overlay", "ZonePetition")
    attributes = await _attributes_at(client, catalog.ZONING, point, fields)
    if not attributes or "ZoneDes" not in attributes:
        return None
    overlay = _opt_str(attributes.get("Overlay"))
    return Zoning(
        district=str(attributes["ZoneDes"]),
        description=_opt_str(attributes.get("ZoneClass")),
        overlay=None if overlay is None or overlay.lower() == "none" else overlay,
        petition=_opt_str(attributes.get("ZonePetition")),
    )


async def _flood_zone(client: ArcGISClient, point: Point) -> str | None:
    attributes = await _attributes_at(client, catalog.FEMA_FLOODPLAIN, point, ("fld_zone",))
    return _opt_str(attributes.get("fld_zone")) if attributes else None


async def _historic_district(client: ArcGISClient, point: Point) -> str | None:
    fields = ("DistrictName", "Name")
    attributes = await _attributes_at(client, catalog.HISTORIC_DISTRICTS, point, fields)
    if not attributes:
        return None
    return _opt_str(attributes.get("DistrictName") or attributes.get("Name"))


async def _zip_code(client: ArcGISClient, point: Point) -> str | None:
    attributes = await _attributes_at(client, catalog.ZIP_CODES, point, ("zip",))
    return _opt_str(attributes.get("zip")) if attributes else None


async def _collection_schedule(client: ArcGISClient, point: Point) -> CollectionSchedule | None:
    result = await client.query(
        catalog.SOLID_WASTE_ROUTES,
        near=point,
        out_fields=("ROUTE_TYPE", "WORK_DAY", "SERVED_BY", "ROUTE_NOTE"),
    )
    days: dict[str, CollectionDay] = {}
    for feature in result.features:
        attributes = feature.attributes
        kind = {"GARB": "garbage", "RECY": "recycling", "YARD": "yard_waste"}.get(
            str(attributes.get("ROUTE_TYPE"))
        )
        if kind and kind not in days:
            day = str(attributes.get("WORK_DAY", ""))
            days[kind] = CollectionDay(
                day=_DAY_NAMES.get(day, day),
                served_by=_opt_str(attributes.get("SERVED_BY")),
                note=_opt_str(attributes.get("ROUTE_NOTE")),
            )
    if not days:
        return None
    return CollectionSchedule(
        garbage=days.get("garbage"),
        recycling=days.get("recycling"),
        yard_waste=days.get("yard_waste"),
    )


async def get_trash_and_recycling_schedule(
    ctx: Context[AppState, Any], location: LocationText
) -> TrashScheduleResult:
    """Garbage, recycling, and yard-waste collection days for an address in Charlotte."""
    client = _client(ctx)
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
        schedule = await _collection_schedule(client, resolved.point)
    notes = ["Recycling is collected every other week: GREEN week or ORANGE week."]
    if schedule is None:
        notes = [
            "No City of Charlotte residential collection route covers this location. It may "
            "be outside city limits or served by a private hauler (e.g. apartments, businesses)."
        ]
    return TrashScheduleResult(location=_location_info(resolved), schedule=schedule, notes=notes)


# --------------------------------------------------------------------------------------
# Tools: public safety
# --------------------------------------------------------------------------------------

_CRIME_GROUPS: Final = {
    "offense": "HIGHEST_NIBRS_DESCRIPTION",
    "patrol_division": "CMPD_PATROL_DIVISION",
    "year": "YEAR",
    "neighborhood_profile_area": "NPA",
    "place_type": "PLACE_TYPE_DESCRIPTION",
    "clearance_status": "CLEARANCE_STATUS",
}
_CRIME_NOTES: Final = (
    "Each record is one CMPD incident report, classified by its most serious NIBRS offense.",
    "CMPD publishes generalized locations to protect privacy, so edge-of-radius results are "
    "approximate.",
)


async def get_crime_near(
    ctx: Context[AppState, Any],
    location: LocationText,
    radius_miles: Annotated[float, Field(gt=0, le=3, description="Radius in miles.")] = 0.5,
    days: Annotated[int, Field(ge=1, le=365, description="How many days back to look.")] = 30,
    offense: Annotated[
        str | None,
        Field(
            max_length=60,
            description="Optional offense filter, e.g. 'burglary', 'motor vehicle theft', "
            "'assault', 'vandalism'.",
        ),
    ] = None,
    limit: Limit = 20,
) -> RecordsResult:
    """CMPD police incident reports near a location: counts by offense and the latest reports.

    Answers "is there much crime near 123 Main St?" or "any car break-ins near me lately?".
    """
    client = _client(ctx)
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
        where, since = _recent("DATE_REPORTED", days)
        if offense:
            where += " AND " + _contains("HIGHEST_NIBRS_DESCRIPTION", offense)
        return await _records_near(
            client,
            catalog.CMPD_INCIDENTS,
            where=where,
            fields=(
                "INCIDENT_REPORT_ID",
                "DATE_REPORTED",
                "DATE_INCIDENT_BEGAN",
                "HIGHEST_NIBRS_DESCRIPTION",
                "LOCATION",
                "LOCATION_TYPE_DESCRIPTION",
                "PLACE_TYPE_DESCRIPTION",
                "PLACE_DETAIL_DESCRIPTION",
                "CLEARANCE_STATUS",
                "CMPD_PATROL_DIVISION",
            ),
            order_by="DATE_REPORTED DESC",
            limit=limit,
            location=resolved,
            radius_miles=radius_miles,
            breakdown_field="HIGHEST_NIBRS_DESCRIPTION",
            since=since,
            notes=_CRIME_NOTES,
        )


async def summarize_crime(
    ctx: Context[AppState, Any],
    group_by: Literal[
        "offense",
        "patrol_division",
        "year",
        "neighborhood_profile_area",
        "place_type",
        "clearance_status",
    ] = "offense",
    year: Annotated[int | None, Field(ge=2017, le=2100, description="Calendar year.")] = None,
    offense: Annotated[str | None, Field(max_length=60, description="e.g. 'robbery'.")] = None,
    patrol_division: Annotated[
        str | None, Field(max_length=60, description="e.g. 'Central', 'Providence'.")
    ] = None,
    limit: Limit = 25,
) -> SummaryResult:
    """Citywide CMPD incident counts grouped by offense, division, year, or neighborhood.

    Use group_by='year' with an offense filter for trends, e.g. robberies per year since 2017.
    """
    clauses = []
    with _user_facing_errors():
        if year is not None and group_by != "year":
            clauses.append(f"YEAR = {sql_quote(str(year))}")
        if offense:
            clauses.append(_contains("HIGHEST_NIBRS_DESCRIPTION", offense))
        if patrol_division:
            clauses.append(_contains("CMPD_PATROL_DIVISION", patrol_division))
        where = " AND ".join(clauses) or "1=1"
        field = _CRIME_GROUPS[group_by]
        rows = await _client(ctx).statistics(
            catalog.CMPD_INCIDENTS,
            statistics=[statistic("count", "OBJECTID", "incidents")],
            group_by=[field],
            where=where,
            order_by="YEAR ASC" if group_by == "year" else "incidents DESC",
            limit=limit,
        )
    return SummaryResult(
        source=catalog.CMPD_INCIDENTS,
        filters=where,
        group_by=[field],
        statistic="count(incidents)",
        rows=rows,
        notes=[_CRIME_NOTES[0], "The current year is partial."],
    )


async def get_traffic_crashes_near(
    ctx: Context[AppState, Any],
    location: LocationText,
    radius_miles: Annotated[float, Field(gt=0, le=3, description="Radius in miles.")] = 0.5,
    days: Annotated[int, Field(ge=1, le=1825, description="How many days back to look.")] = 90,
    limit: Limit = 20,
) -> RecordsResult:
    """Reported traffic crashes near a location, with counts by severity.

    Useful for "is this intersection dangerous?" or traffic-calming requests.
    """
    client = _client(ctx)
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
        where, since = _recent("DATE_VAL", days)
        return await _records_near(
            client,
            catalog.CRASHES,
            where=where,
            fields=("DATE_VAL", "CRASH_TYPE", "CRSH_LEVL_DESC", "DAY_OF_WEEK_DESC", "MILT_TIME"),
            order_by="DATE_VAL DESC",
            limit=limit,
            location=resolved,
            radius_miles=radius_miles,
            breakdown_field="CRSH_LEVL_DESC",
            since=since,
            notes=("MILT_TIME is the 24-hour clock time of the crash.",),
        )


# --------------------------------------------------------------------------------------
# Tools: city services and neighborhood change
# --------------------------------------------------------------------------------------


async def get_311_requests_near(
    ctx: Context[AppState, Any],
    location: LocationText,
    radius_miles: Annotated[float, Field(gt=0, le=2, description="Radius in miles.")] = 0.25,
    days: Annotated[int, Field(ge=1, le=365, description="How many days back to look.")] = 30,
    request_type: Annotated[
        str | None,
        Field(
            max_length=60,
            description="Optional type filter, e.g. 'pothole', 'missed recycling', 'graffiti', "
            "'streetlight', 'dumping'.",
        ),
    ] = None,
    limit: Limit = 20,
) -> RecordsResult:
    """311 service requests reported near a location, with counts by request type.

    Shows whether a problem (pothole, missed pickup, dumping, streetlight) was already reported.
    """
    client = _client(ctx)
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
        where, since = _recent("RECEIVED_DATE", days)
        if request_type:
            where += " AND " + _contains("REQUEST_TYPE", request_type)
        return await _records_near(
            client,
            catalog.SERVICE_REQUESTS_311,
            where=where,
            fields=(
                "REQUEST_NO",
                "REQUEST_TYPE",
                "TITLE",
                "DEPARTMENT",
                "DIVISION",
                "RECEIVED_DATE",
                "FULL_ADDRESS",
                "INTERNAL_FIELD_OBSERVATION",
            ),
            order_by="RECEIVED_DATE DESC",
            limit=limit,
            location=resolved,
            radius_miles=radius_miles,
            breakdown_field="REQUEST_TYPE",
            since=since,
            notes=("This dataset records when requests were received, not whether resolved.",),
        )


async def get_code_enforcement_cases(
    ctx: Context[AppState, Any],
    location: LocationText,
    radius_miles: Annotated[
        float, Field(gt=0, le=2, description="Radius in miles; 0.05 is about one parcel.")
    ] = 0.1,
    status: Literal["open", "closed", "any"] = "any",
    case_type: Literal[
        "any", "Nuisance", "Housing", "Zoning", "Graffiti", "Parking", "Commercial"
    ] = "any",
    days: Annotated[int, Field(ge=1, le=3650, description="Cases opened in this many days.")] = 365,
    limit: Limit = 20,
) -> RecordsResult:
    """Housing and neighborhood code enforcement cases at or near an address.

    Covers nuisance (junk, overgrown lots), minimum housing, zoning, graffiti, and parking
    violations. Useful for renters, buyers, and neighbors.
    """
    client = _client(ctx)
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
        where, since = _recent("DateCreated", days)
        if status == "open":
            where += " AND CaseStatus IN ('Open', 'New')"
        elif status == "closed":
            where += " AND CaseStatus = 'Closed'"
        if case_type != "any":
            where += f" AND CaseType = {sql_quote(case_type)}"
        return await _records_near(
            client,
            catalog.CODE_ENFORCEMENT,
            where=where,
            fields=(
                "CaseNumber",
                "CaseType",
                "CaseStatus",
                "FullAddress",
                "DateCreated",
                "DateClosed",
                "DetailedDescription",
                "Conclusion",
                "CaseOrigin",
                "Inspector",
                "InspectorPhone",
                "ReqNum311",
            ),
            order_by="DateCreated DESC",
            limit=limit,
            location=resolved,
            radius_miles=radius_miles,
            breakdown_field="CaseType",
            since=since,
        )


async def get_street_closures(
    ctx: Context[AppState, Any],
    near: OptionalLocationText = None,
    radius_miles: Annotated[float, Field(gt=0, le=15, description="Used with near.")] = 2,
    active_only: bool = True,
    limit: Limit = 25,
) -> RecordsResult:
    """Street and lane closures and detours published by CDOT, citywide or near a location."""
    client = _client(ctx)
    with _user_facing_errors():
        location = await resolve_location(client, near) if near else None
        return await _records_near(
            client,
            catalog.STREET_CLOSURES,
            where="ACTIVE = 'Yes'" if active_only else "1=1",
            fields=(
                "LOCDESC",
                "BLOCKNM",
                "BLOCKTYPE",
                "ClosureType",
                "FULLCLOSE",
                "DIRECTION",
                "STARTDATE",
                "ENDDATE",
                "COMMENT",
                "ALTROUTE",
                "ResponsibleDepartment",
                "CATS",
                "Hyperlink",
                "ClosureMapURL",
                "SpecialProject",
            ),
            order_by="STARTDATE DESC",
            limit=limit,
            location=location,
            radius_miles=radius_miles,
            breakdown_field="ClosureType",
        )


async def get_capital_projects_near(
    ctx: Context[AppState, Any],
    location: LocationText,
    radius_miles: Annotated[float, Field(gt=0, le=5, description="Radius in miles.")] = 1,
    phase: Literal[
        "any", "Planning", "Design", "Construction", "Active", "On Hold", "Complete"
    ] = "any",
    limit: Limit = 15,
) -> RecordsResult:
    """City capital projects (roads, sidewalks, parks, facilities) near a location.

    Includes phase, schedule, budget, and the project manager's contact information.
    """
    client = _client(ctx)
    fields = (
        "Project_ID", "Project_Name", "Project_Type", "Project_Phase", "Location_Description",
        "Public_Project_Description", "Anticipated_Start_Date", "Anticipated_Compl_Date",
        "Total_Project_Budget", "Controlling_Entity", "Project_Manager", "Project_Manager_Email",
        "Project_Manager_Phone", "Existing_URL",
    )  # fmt: skip
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
        where = "1=1" if phase == "any" else f"Project_Phase = {sql_quote(phase)}"
        async with _task_group() as group:
            tasks = [
                group.create_task(
                    client.query(
                        url,
                        where=where,
                        out_fields=fields,
                        near=resolved.point,
                        radius_miles=radius_miles,
                        limit=500,
                        return_geometry=True,
                    )
                )
                for url in (catalog.CAPITAL_PROJECT_POINTS, catalog.CAPITAL_PROJECT_AREAS)
            ]
    projects: dict[str, dict[str, Any]] = {}
    for task in tasks:
        for feature in task.result().features:
            record = _with_distance(feature, resolved.point)
            key = str(record.get("Project_ID") or record.get("Project_Name"))
            if key not in projects or _distance_key(record) < _distance_key(projects[key]):
                projects[key] = record
    ordered = sorted(projects.values(), key=_distance_key)
    phases = Counter(str(record.get("Project_Phase", "(blank)")) for record in ordered)
    return RecordsResult(
        location=_location_info(resolved),
        radius_miles=radius_miles,
        total_matching=len(ordered),
        breakdown_by="Project_Phase",
        breakdown=[CountByValue(value=value, count=n) for value, n in phases.most_common()],
        records=ordered[:limit],
        notes=["For project areas, distance is measured to the nearest boundary vertex."],
    )


async def get_pending_rezonings(
    ctx: Context[AppState, Any],
    near: OptionalLocationText = None,
    radius_miles: Annotated[float, Field(gt=0, le=10, description="Used with near.")] = 1,
    limit: Limit = 20,
) -> RecordsResult:
    """Rezoning petitions currently in process, citywide or near a location.

    Shows the petitioner, existing and requested zoning, acreage, and a link to the petition.
    """
    client = _client(ctx)
    with _user_facing_errors():
        location = await resolve_location(client, near) if near else None
        return await _records_near(
            client,
            catalog.REZONINGS,
            where="1=1",
            fields=(
                "Petition",
                "Petitioner",
                "ExistZone",
                "ReqZone",
                "Type",
                "Status",
                "Acres",
                "Received",
                "Approved",
                "Hyperlink",
            ),
            order_by="Received DESC",
            limit=limit,
            location=location,
            radius_miles=radius_miles,
            notes=("Status 'Pen' means pending.",),
        )


# --------------------------------------------------------------------------------------
# Tools: places and transit
# --------------------------------------------------------------------------------------


async def find_nearby_places(
    ctx: Context[AppState, Any],
    category: catalog.PlaceCategory,
    location: LocationText,
    radius_miles: Annotated[float, Field(gt=0, le=15, description="Radius in miles.")] = 2,
    limit: Annotated[int, Field(ge=1, le=50, description="Maximum places to return.")] = 10,
) -> NearbyPlacesResult:
    """Nearest public places of one type, sorted by straight-line distance.

    Categories include libraries, public schools, parks, greenways, fire and police stations,
    post offices, pharmacies, grocery stores, medical facilities, child care, EV chargers,
    park-and-ride lots, light rail stations, bus stops, YMCAs, places of worship, and public
    Wi-Fi. Bus stop IDs can be used with CATS real-time arrival services.
    """
    client = _client(ctx)
    layers = catalog.PLACE_LAYERS[category]
    with _user_facing_errors():
        resolved = await resolve_location(client, location)
        async with _task_group() as group:
            tasks = [
                (
                    layer,
                    group.create_task(
                        client.query(
                            layer.url,
                            where=layer.where,
                            out_fields=layer.fields,
                            near=resolved.point,
                            radius_miles=radius_miles,
                            limit=1000,
                            return_geometry=True,
                        )
                    ),
                )
                for layer in layers
            ]
    places: list[dict[str, Any]] = []
    truncated = False
    for layer, task in tasks:
        result = task.result()
        truncated |= result.exceeded_transfer_limit
        layer_places = [_place(layer, feature, resolved.point) for feature in result.features]
        if layer.one_result_per_name:
            nearest: dict[str, dict[str, Any]] = {}
            for place in sorted(layer_places, key=_distance_key):
                nearest.setdefault(str(place["name"]), place)
            layer_places = list(nearest.values())
        places.extend(layer_places)
    places.sort(key=_distance_key)
    notes = ["Distances are straight-line, not travel distance."]
    if truncated:
        notes.append("More places matched than could be fetched; use a smaller radius.")
    return NearbyPlacesResult(
        location=_location_info(resolved),
        category=category,
        radius_miles=radius_miles,
        total_within_radius=len(places),
        places=places[:limit],
        notes=notes,
    )


def _place(layer: catalog.PlaceLayer, feature: Feature, point: Point) -> dict[str, Any]:
    record = _with_distance(feature, point)
    name = record.pop(layer.name_field, None)
    return {"name": name or "(unnamed)", "source": layer.source, **record}


async def get_bus_route(
    ctx: Context[AppState, Any],
    route: Annotated[
        str | None,
        Field(
            max_length=60,
            description="Route number (e.g. '9') or part of its name; omit to list all routes.",
        ),
    ] = None,
) -> RecordsResult:
    """CATS bus routes with scheduled frequency (minutes between buses) by time of day.

    For stops near an address use find_nearby_places with category 'bus_stop'.
    """
    client = _client(ctx)
    with _user_facing_errors():
        if route and route.strip():
            text = route.strip()
            where = f"Route = {sql_quote(text.upper())} OR {_contains('Route_Name', text)}"
            fields: tuple[str, ...] = ("*",)
        else:
            where, fields = "1=1", ("Route", "Route_Name", "Route_Type")
        result = await client.query(
            catalog.BUS_ROUTES, where=where, out_fields=fields, order_by="Route", limit=100
        )
    return RecordsResult(
        total_matching=len(result.features),
        records=[feature.attributes for feature in result.features],
        notes=["Frequencies are scheduled headways; live arrivals are not in this dataset."],
    )


# --------------------------------------------------------------------------------------
# Tools: government transparency
# --------------------------------------------------------------------------------------

_BUDGET_GROUPS: Final = {
    "department": "Department_Name",
    "fund": "Fund_Name",
    "expense_category": "Object_Name",
}
_SALARY_GROUPS: Final = {"department": "Dept", "job_title": "Job_Title"}


async def get_city_budget(
    ctx: Context[AppState, Any],
    fiscal_year: Annotated[
        str | None,
        Field(pattern=r"^FY\d{4}$", description="e.g. 'FY2023'; defaults to the latest published."),
    ] = None,
    group_by: Literal["department", "fund", "expense_category"] = "department",
    department: Annotated[
        str | None, Field(max_length=80, description="Optional filter, e.g. 'Police'.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
) -> BudgetResult:
    """City of Charlotte budget totals for a fiscal year by department, fund, or expense type."""
    client = _client(ctx)
    with _user_facing_errors():
        year_rows = await client.statistics(
            catalog.BUDGET,
            statistics=[statistic("count", "OBJECTID", _COUNT_ALIAS)],
            group_by=["Fiscal_Year"],
            order_by="Fiscal_Year DESC",
        )
        years = [str(row["Fiscal_Year"]) for row in year_rows if row.get("Fiscal_Year")]
        if not years:
            raise ArcGISError("The budget dataset returned no fiscal years")
        chosen = fiscal_year or years[0]
        if chosen not in years:
            raise ValueError(f"{chosen} is not published; available: {', '.join(years)}.")
        where = f"Fiscal_Year = {sql_quote(chosen)}"
        if department:
            where += " AND " + _contains("Department_Name", department)
        field = _BUDGET_GROUPS[group_by]
        amount = [statistic("sum", "Amount", "total_amount")]
        async with _task_group() as group:
            grouped = group.create_task(
                client.statistics(
                    catalog.BUDGET,
                    statistics=amount,
                    group_by=[field],
                    where=where,
                    order_by="total_amount DESC",
                    limit=limit,
                )
            )
            net = group.create_task(
                client.statistics(catalog.BUDGET, statistics=amount, where=where)
            )
            positive = group.create_task(
                client.statistics(
                    catalog.BUDGET, statistics=amount, where=f"{where} AND Amount > 0"
                )
            )
    return BudgetResult(
        fiscal_year=chosen,
        available_fiscal_years=years,
        group_by=field,
        department_filter=department,
        net_total_amount=_total_amount(net.result()),
        positive_total_amount=_total_amount(positive.result()),
        rows=[
            BudgetRow(
                name=str(row.get(field, "(blank)")),
                amount=_opt_float(row.get("total_amount")) or 0.0,
            )
            for row in grouped.result()
        ],
        notes=[
            "Amounts are from the portal's Budget Report dataset, in US dollars.",
            "Some lines are large negative amounts (e.g. '00 Non Department', Charlotte Water), "
            "so the net total is far below the sum of department budgets.",
        ],
    )


def _total_amount(rows: list[dict[str, Any]]) -> float:
    return (_opt_float(rows[0].get("total_amount")) if rows else None) or 0.0


async def get_city_salary_stats(
    ctx: Context[AppState, Any],
    group_by: Literal["department", "job_title"] = "department",
    department: Annotated[
        str | None,
        Field(
            max_length=80,
            description="Partial match on abbreviated department names, e.g. 'CFD' (Fire), "
            "'CMPD' (Police), 'CDOT', 'CATS', 'Charlotte Water'.",
        ),
    ] = None,
    job_title: Annotated[
        str | None,
        Field(max_length=80, description="Partial match, e.g. 'Fire Fighter' or 'Captain'."),
    ] = None,
    year: Annotated[int | None, Field(ge=2019, le=2100)] = None,
    quarter: Annotated[int | None, Field(ge=1, le=4)] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
) -> SalaryResult:
    """Pay statistics for City of Charlotte employees: head count and annual pay rates.

    Aggregated by department or job title for one quarter (the latest by default); individual
    employees are not listed.
    """
    client = _client(ctx)
    with _user_facing_errors():
        period_filter = f"Year = {year}" if year is not None else "1=1"
        if quarter is not None:
            period_filter += f" AND Quarter = {quarter}"
        periods = await client.statistics(
            catalog.SALARIES,
            statistics=[statistic("count", "OBJECTID", _COUNT_ALIAS)],
            group_by=["Year", "Quarter"],
            where=period_filter,
            order_by="Year DESC, Quarter DESC",
            limit=1,
        )
        if not periods:
            raise ValueError("No salary data is published for that year and quarter.")
        chosen_year, chosen_quarter = int(periods[0]["Year"]), int(periods[0]["Quarter"])
        where = f"Year = {chosen_year} AND Quarter = {chosen_quarter}"
        if department:
            where += " AND " + _contains("Dept", department)
        if job_title:
            where += " AND " + _contains("Job_Title", job_title)
        field = _SALARY_GROUPS[group_by]
        pay = [
            statistic("count", "OBJECTID", "employees"),
            statistic("avg", "Annual_Rt", "average_rate"),
            statistic("min", "Annual_Rt", "minimum_rate"),
            statistic("max", "Annual_Rt", "maximum_rate"),
        ]
        async with _task_group() as group:
            grouped = group.create_task(
                client.statistics(
                    catalog.SALARIES,
                    statistics=pay,
                    group_by=[field],
                    where=where,
                    order_by="employees DESC",
                    limit=limit,
                )
            )
            overall = group.create_task(
                client.statistics(catalog.SALARIES, statistics=pay, where=where)
            )
    overall_rows = overall.result()
    rows = [_salary_row(str(row.get(field, "(blank)")), row) for row in grouped.result()]
    notes = ["Annual_Rt is the annualized pay rate, not total compensation or overtime."]
    if not rows:
        notes.append(
            "No employees matched. Try a shorter term: titles look like 'Fire Fighter II' and "
            "departments like 'CFD Operations' or 'CMPD Patrol Serv Group'."
        )
    return SalaryResult(
        year=chosen_year,
        quarter=chosen_quarter,
        group_by=field,
        filters=where,
        overall=_salary_row("All matching employees", overall_rows[0] if overall_rows else {}),
        rows=rows,
        notes=notes,
    )


def _salary_row(group: str, row: dict[str, Any]) -> SalaryRow:
    return SalaryRow(
        group=group,
        employees=_opt_int(row.get("employees")) or 0,
        average_annual_rate=_opt_float(row.get("average_rate")),
        minimum_annual_rate=_opt_float(row.get("minimum_rate")),
        maximum_annual_rate=_opt_float(row.get("maximum_rate")),
    )


# --------------------------------------------------------------------------------------
# Server assembly
# --------------------------------------------------------------------------------------

_TOOLS: Final[tuple[tuple[Callable[..., Coroutine[Any, Any, BaseModel]], str], ...]] = (
    (get_address_profile, "Address profile: representatives, services, zoning"),
    (lookup_address, "Look up a Mecklenburg County address"),
    (get_trash_and_recycling_schedule, "Trash and recycling schedule"),
    (get_crime_near, "Police incidents near a location"),
    (summarize_crime, "Citywide crime statistics"),
    (get_traffic_crashes_near, "Traffic crashes near a location"),
    (get_311_requests_near, "311 service requests near a location"),
    (get_code_enforcement_cases, "Code enforcement cases near an address"),
    (get_street_closures, "Street closures and detours"),
    (get_capital_projects_near, "City capital projects near a location"),
    (get_pending_rezonings, "Pending rezoning petitions"),
    (find_nearby_places, "Find nearby public places"),
    (get_bus_route, "CATS bus route frequencies"),
    (get_city_budget, "City budget by department or fund"),
    (get_city_salary_stats, "City employee pay statistics"),
    (search_datasets, "Search Charlotte open datasets"),
    (describe_dataset, "Describe a dataset's fields"),
    (query_dataset, "Query any dataset"),
    (summarize_dataset, "Aggregate any dataset"),
)


def create_server() -> MCPServer[AppState]:
    """Build the MCP server with every tool registered as read-only."""
    server: MCPServer[AppState] = MCPServer(
        "queensestate",
        title="QueensEstate",
        instructions=INSTRUCTIONS,
        website_url=PORTAL_URL,
        version=__version__,
        lifespan=_lifespan,
    )
    for tool, title in _TOOLS:
        server.tool(title=title, annotations=_READ_ONLY)(tool)
    return server


def main() -> None:
    """Console entry point: serve over stdio (default) or Streamable HTTP."""
    parser = argparse.ArgumentParser(description="QueensEstate MCP server")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    args = parser.parse_args()
    # httpx logs every request URL at INFO, which drowns out the server's own logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server = create_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
