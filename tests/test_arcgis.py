from datetime import UTC, datetime, timedelta, timezone
from typing import get_args

import httpx
import pytest
import respx

from queensestate import catalog
from queensestate.arcgis import (
    ArcGISClient,
    ArcGISError,
    clean_attributes,
    epoch_ms_to_iso,
    html_to_text,
    sql_quote,
    sql_timestamp,
    validate_layer_url,
)
from queensestate.geo import Point

LAYER = "https://gis.charlottenc.gov/arcgis/rest/services/CMPD/CMPDIncidents/MapServer/0"


@pytest.mark.parametrize(
    "url",
    [
        LAYER,
        LAYER + "/",
        "https://services.arcgis.com/9Nl857LBlQVyzq54/arcgis/rest/services/Bus_Stops/FeatureServer/0",
        "https://meckgis.mecklenburgcountync.gov/server/rest/services/ParkBoundaries/FeatureServer/0",
    ],
)
def test_validate_layer_url_accepts_portal_layers(url: str) -> None:
    assert validate_layer_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        LAYER.replace("https", "http"),
        "https://evil.example.com/arcgis/rest/services/X/MapServer/0",
        LAYER + "?f=json",
        LAYER + "#frag",
        "https://gis.charlottenc.gov:8443/arcgis/rest/services/CMPD/CMPDIncidents/MapServer/0",
        "https://user:pw@gis.charlottenc.gov/arcgis/rest/services/CMPD/CMPDIncidents/MapServer/0",
        "https://gis.charlottenc.gov/arcgis/rest/services/CMPD/CMPDIncidents/MapServer",
        "https://gis.charlottenc.gov/arcgis/rest/services/../admin/MapServer/0",
        "https://gis.charlottenc.gov/arcgis/admin/MapServer/0",
    ],
)
def test_validate_layer_url_rejects_everything_else(url: str) -> None:
    with pytest.raises(ValueError, match="ArcGIS layer URL"):
        validate_layer_url(url)


def test_every_catalog_url_is_allowlisted() -> None:
    urls = [
        value
        for name, value in vars(catalog).items()
        if not name.startswith("_") and name.isupper() and "http" in value
    ]
    urls += [layer.url for layers in catalog.PLACE_LAYERS.values() for layer in layers]
    for url in urls:
        assert validate_layer_url(url) == url


def test_place_category_literal_matches_layers() -> None:
    assert set(get_args(catalog.PlaceCategory)) == set(catalog.PLACE_LAYERS)


def test_sql_helpers() -> None:
    assert sql_quote("O'Hara") == "'O''Hara'"
    eastern = timezone(timedelta(hours=-4))
    assert (
        sql_timestamp(datetime(2026, 9, 1, 8, tzinfo=eastern)) == "TIMESTAMP '2026-09-01 12:00:00'"
    )


def test_epoch_ms_to_iso() -> None:
    assert epoch_ms_to_iso(1788825600000) == "2026-09-07T20:00:00-04:00"
    assert epoch_ms_to_iso(1733325715000) == "2024-12-04T10:21:55-05:00"  # standard time
    assert epoch_ms_to_iso(True) is True
    assert epoch_ms_to_iso("x") == "x"
    assert epoch_ms_to_iso(1e20) == 1e20


def test_clean_attributes_drops_bookkeeping_and_formats_dates() -> None:
    raw = {
        "OBJECTID": 1,
        "GlobalID": "{abc}",
        "Shape__Area": 3.2,
        "SHAPE.STLength()": 9.1,
        "Name": "  Main Library ",
        "Blank": "   ",
        "Missing": None,
        "Opened": 1788825600000,
        "Visits": 12,
    }
    assert clean_attributes(raw, frozenset({"Opened"})) == {
        "Name": "Main Library",
        "Opened": "2026-09-07T20:00:00-04:00",
        "Visits": 12,
    }


def test_html_to_text() -> None:
    assert html_to_text("<p>Fish &amp; <b>chips</b></p>") == "Fish & chips"
    assert html_to_text("word " * 50, max_length=20).endswith("…")
    assert len(html_to_text("word " * 50, max_length=20)) <= 20


@pytest.mark.anyio
async def test_query_sends_spatial_filter_and_parses_features() -> None:
    payload = {
        "fields": [{"name": "DATE_REPORTED", "type": "esriFieldTypeDate"}],
        "features": [
            {
                "attributes": {"OBJECTID": 7, "DATE_REPORTED": 1788825600000, "ZIP": "28202"},
                "geometry": {"x": -80.84, "y": 35.22},
            }
        ],
        "exceededTransferLimit": True,
    }
    with respx.mock:
        route = respx.get(f"{LAYER}/query").mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            result = await ArcGISClient(http).query(
                LAYER,
                where="1=1",
                out_fields=("ZIP", "DATE_REPORTED"),
                near=Point(35.22, -80.84),
                radius_miles=0.5,
                order_by="DATE_REPORTED DESC",
                limit=5,
                return_geometry=True,
            )
    params = route.calls.last.request.url.params
    assert params["geometry"] == "-80.84,35.22"
    assert params["inSR"] == "4326"
    assert params["outSR"] == "4326"
    assert params["distance"] == "0.5"
    assert params["units"] == "esriSRUnit_StatuteMile"
    assert params["outFields"] == "ZIP,DATE_REPORTED"
    assert params["resultRecordCount"] == "5"
    assert result.exceeded_transfer_limit
    assert result.features[0].attributes == {
        "DATE_REPORTED": "2026-09-07T20:00:00-04:00",
        "ZIP": "28202",
    }
    assert result.features[0].geometry == {"x": -80.84, "y": 35.22}


@pytest.mark.anyio
async def test_arcgis_error_envelope_becomes_arcgis_error() -> None:
    error = {"error": {"code": 400, "message": "Invalid query", "details": ["bad field FOO"]}}
    with respx.mock:
        respx.get(f"{LAYER}/query").mock(return_value=httpx.Response(200, json=error))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ArcGISError, match=r"Invalid query \(bad field FOO\)"):
                await ArcGISClient(http).count(LAYER)


@pytest.mark.anyio
async def test_transient_status_is_retried_then_succeeds() -> None:
    with respx.mock:
        route = respx.get(f"{LAYER}/query").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"count": 4})]
        )
        async with httpx.AsyncClient() as http:
            assert await ArcGISClient(http).count(LAYER) == 4
    assert route.call_count == 2


@pytest.mark.anyio
async def test_transport_errors_give_up_after_max_retries() -> None:
    with respx.mock:
        route = respx.get(f"{LAYER}/query").mock(side_effect=httpx.ConnectTimeout("slow"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ArcGISError, match=r"Could not reach gis\.charlottenc\.gov"):
                await ArcGISClient(http, max_retries=2).count(LAYER)
    assert route.call_count == 3


@pytest.mark.anyio
async def test_client_errors_are_not_retried() -> None:
    with respx.mock:
        route = respx.get(f"{LAYER}/query").mock(return_value=httpx.Response(403))
        async with httpx.AsyncClient() as http:
            with pytest.raises(ArcGISError, match="HTTP 403"):
                await ArcGISClient(http).count(LAYER)
    assert route.call_count == 1


@pytest.mark.anyio
async def test_non_json_and_missing_count_are_errors() -> None:
    with respx.mock:
        respx.get(f"{LAYER}/query").mock(
            side_effect=[httpx.Response(200, text="<html>"), httpx.Response(200, json={})]
        )
        async with httpx.AsyncClient() as http:
            client = ArcGISClient(http)
            with pytest.raises(ArcGISError, match="not JSON"):
                await client.count(LAYER)
            with pytest.raises(ArcGISError, match="did not return a record count"):
                await client.count(LAYER)


@pytest.mark.anyio
async def test_statistics_enforce_limit_when_the_service_ignores_it() -> None:
    rows = [{"attributes": {"DIVISION": f"D{n}", "n": n}} for n in range(5)]
    with respx.mock:
        route = respx.get(f"{LAYER}/query").mock(
            return_value=httpx.Response(200, json={"features": rows})
        )
        async with httpx.AsyncClient() as http:
            result = await ArcGISClient(http).statistics(
                LAYER, statistics=[], group_by=["DIVISION"], limit=2
            )
    assert result == [{"DIVISION": "D0", "n": 0}, {"DIVISION": "D1", "n": 1}]
    assert route.calls.last.request.url.params["resultRecordCount"] == "2"


def test_timestamps_are_utc_aware() -> None:
    assert sql_timestamp(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)).endswith("03:04:05'")
