from typing import Any

import httpx
import pytest
from conftest import FakeArcGIS, arcgis_response, feature, statistics_request
from mcp import Client
from mcp.types import CallToolResult

from queensestate import catalog
from queensestate.arcgis import HUB_SEARCH_URL
from queensestate.server import create_server

pytestmark = pytest.mark.anyio

DOWNTOWN = "35.2217,-80.8390"
EXPECTED_TOOLS = {
    "get_address_profile", "lookup_address", "get_trash_and_recycling_schedule",
    "get_crime_near", "summarize_crime", "get_traffic_crashes_near", "get_311_requests_near",
    "get_code_enforcement_cases", "get_street_closures", "get_capital_projects_near",
    "get_pending_rezonings", "find_nearby_places", "get_bus_route", "get_city_budget",
    "get_city_salary_stats", "search_datasets", "describe_dataset", "query_dataset",
    "summarize_dataset",
}  # fmt: skip
SOLID_WASTE_ROUTES = [
    feature({"ROUTE_TYPE": "GARB", "WORK_DAY": "WED", "SERVED_BY": "SOLID WASTE SERVICES",
             "ROUTE_NOTE": "WED Collection for Garbage"}),
    feature({"ROUTE_TYPE": "RECY", "WORK_DAY": "WED", "SERVED_BY": "WASTE MANAGEMENT",
             "ROUTE_NOTE": "WED Collection for Recycling on GREEN week"}),
    feature({"ROUTE_TYPE": "YARD", "WORK_DAY": "WED", "SERVED_BY": "SOLID WASTE SERVICES"}),
]  # fmt: skip


async def call(name: str, arguments: dict[str, Any]) -> CallToolResult:
    async with Client(create_server()) as client:
        return await client.call_tool(name, arguments)


def structured(result: CallToolResult) -> dict[str, Any]:
    assert not result.is_error, result.content
    data: dict[str, Any] | None = result.structured_content
    assert data is not None
    return data


def error_text(result: CallToolResult) -> str:
    assert result.is_error
    return " ".join(getattr(block, "text", "") for block in result.content)


def serve_features(fake: FakeArcGIS, layer_url: str, features: list[dict[str, Any]]) -> None:
    fake.serve(layer_url, lambda request: arcgis_response(request, features))


async def test_tools_are_registered_read_only_with_descriptions(fake_arcgis: FakeArcGIS) -> None:
    async with Client(create_server()) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description
        assert tool.annotations is not None
        hints = tool.annotations.model_dump(by_alias=True)
        assert hints["readOnlyHint"] is True
        assert hints["destructiveHint"] is False
        assert "ctx" not in tool.input_schema.get("properties", {})


async def test_trash_schedule_geocodes_the_address(fake_arcgis: FakeArcGIS) -> None:
    serve_features(
        fake_arcgis,
        catalog.MASTER_ADDRESS,
        [feature({"FullAddress": "600 E 4TH ST CHARLOTTE NC 28202", "StreetName": "4TH",
                  "Direction": "E", "StreetType": "ST"}, -80.8396, 35.2212)],
    )  # fmt: skip
    serve_features(fake_arcgis, catalog.SOLID_WASTE_ROUTES, SOLID_WASTE_ROUTES)

    data = structured(await call("get_trash_and_recycling_schedule", {"location": "600 E 4th St"}))

    assert data["location"]["matched_address"] == "600 E 4TH ST CHARLOTTE NC 28202"
    assert data["schedule"]["garbage"]["day"] == "Wednesday"
    assert "GREEN week" in data["schedule"]["recycling"]["note"]
    assert data["schedule"]["yard_waste"]["served_by"] == "SOLID WASTE SERVICES"
    params = fake_arcgis.requests_to(catalog.SOLID_WASTE_ROUTES)[0].url.params
    assert params["geometry"] == "-80.8396,35.2212"
    assert params["spatialRel"] == "esriSpatialRelIntersects"


async def test_trash_schedule_explains_missing_routes(fake_arcgis: FakeArcGIS) -> None:
    serve_features(fake_arcgis, catalog.SOLID_WASTE_ROUTES, [])
    data = structured(await call("get_trash_and_recycling_schedule", {"location": DOWNTOWN}))
    assert data["schedule"] is None
    assert "private hauler" in data["notes"][0]


async def test_address_profile_combines_sources_and_reports_failures(
    fake_arcgis: FakeArcGIS,
) -> None:
    serve_features(
        fake_arcgis,
        catalog.COUNCIL_DISTRICTS,
        [feature({"District": "1", "DistrictRep": "Rep One", "RepEmail": "one@charlottenc.gov"})],
    )
    serve_features(
        fake_arcgis,
        catalog.COMMISSIONER_DISTRICTS,
        [feature({"longname": "District 4", "cc_name": "Commissioner Four"})],
    )
    serve_features(fake_arcgis, catalog.POLICE_DIVISIONS, [feature({"DNAME": "Central Division"})])
    serve_features(
        fake_arcgis, catalog.FIRE_STATION_AREAS, [feature({"Station": 1, "Battalion": 1})]
    )

    def fire_station(request: httpx.Request) -> httpx.Response:
        assert request.url.params["where"] == "NUM = 1"
        return arcgis_response(request, [feature({"NAME": "Station 1", "ADDRESS": "221 N Myers"})])

    fake_arcgis.serve(catalog.FIRE_STATIONS, fire_station)
    serve_features(fake_arcgis, catalog.SOLID_WASTE_ROUTES, SOLID_WASTE_ROUTES)
    serve_features(
        fake_arcgis,
        catalog.ZONING,
        [feature({"ZoneDes": "UC", "ZoneClass": "UPTOWN MIXED USE", "Overlay": "none"})],
    )
    serve_features(fake_arcgis, catalog.HISTORIC_DISTRICTS, [])
    serve_features(fake_arcgis, catalog.ZIP_CODES, [feature({"zip": "28202"})])
    fake_arcgis.serve(catalog.FEMA_FLOODPLAIN, lambda _request: httpx.Response(503))

    data = structured(await call("get_address_profile", {"location": DOWNTOWN}))

    assert data["city_council"] == {
        "district": "1", "representative": "Rep One", "email": "one@charlottenc.gov",
    }  # fmt: skip
    assert data["county_commissioner"]["commissioner"] == "Commissioner Four"
    assert data["police_division"] == "Central Division"
    assert data["fire_station"] == {
        "station_number": 1, "battalion": 1, "name": "Station 1", "address": "221 N Myers",
    }  # fmt: skip
    assert data["trash_and_recycling"]["garbage"]["day"] == "Wednesday"
    assert data["zoning"] == {
        "district": "UC", "description": "UPTOWN MIXED USE", "overlay": None, "petition": None,
    }  # fmt: skip
    assert data["historic_district"] is None
    assert data["zip_code"] == "28202"
    assert data["fema_flood_zone"] is None
    assert data["unavailable"] == ["fema_flood_zone"]
    assert len(fake_arcgis.requests_to(catalog.FEMA_FLOODPLAIN)) == 3  # first try + 2 retries


async def test_crime_near_filters_by_date_offense_and_radius(fake_arcgis: FakeArcGIS) -> None:
    incidents = [
        feature(
            {"INCIDENT_REPORT_ID": "2026-001", "DATE_REPORTED": 1788825600000,
             "HIGHEST_NIBRS_DESCRIPTION": "Burglary/B&E"},
            -80.8391, 35.2218,
        )
    ]  # fmt: skip
    fake_arcgis.serve(
        catalog.CMPD_INCIDENTS,
        lambda request: arcgis_response(
            request,
            incidents,
            statistics=[{"HIGHEST_NIBRS_DESCRIPTION": "Burglary/B&E", "record_count": 1}],
            date_fields=("DATE_REPORTED",),
        ),
    )

    data = structured(
        await call(
            "get_crime_near",
            {"location": DOWNTOWN, "radius_miles": 0.5, "days": 14, "offense": "burglary"},
        )
    )

    assert data["total_matching"] == 1
    assert data["breakdown"] == [{"value": "Burglary/B&E", "count": 1}]
    record = data["records"][0]
    assert record["DATE_REPORTED"] == "2026-09-07T20:00:00-04:00"
    assert record["distance_miles"] < 0.05
    assert data["since"].endswith(("-04:00", "-05:00"))
    requests = fake_arcgis.requests_to(catalog.CMPD_INCIDENTS)
    assert len(requests) == 3  # count, records, breakdown
    for request in requests:
        params = request.url.params
        assert "DATE_REPORTED >= TIMESTAMP '" in params["where"]
        assert "UPPER(HIGHEST_NIBRS_DESCRIPTION) LIKE '%BURGLARY%'" in params["where"]
        assert params["distance"] == "0.5"
        assert params["units"] == "esriSRUnit_StatuteMile"
    stats = [statistics_request(r) for r in requests if "outStatistics" in r.url.params]
    assert stats == [[{"statisticType": "count", "onStatisticField": "OBJECTID",
                       "outStatisticFieldName": "record_count"}]]  # fmt: skip


async def test_offense_filter_strips_sql_metacharacters(fake_arcgis: FakeArcGIS) -> None:
    fake_arcgis.serve(catalog.CMPD_INCIDENTS, lambda request: arcgis_response(request, []))
    offense = "theft' OR '1'='1"
    structured(await call("get_crime_near", {"location": DOWNTOWN, "offense": offense}))
    where = fake_arcgis.requests_to(catalog.CMPD_INCIDENTS)[0].url.params["where"]
    assert "LIKE '%THEFT OR 1 1%'" in where


async def test_find_nearby_places_sorts_by_distance_and_limits(fake_arcgis: FakeArcGIS) -> None:
    layer = catalog.PLACE_LAYERS["library"][0]
    serve_features(
        fake_arcgis,
        layer.url,
        [
            feature({"Name": "Far Branch", "Address": "2 Far St"}, -80.8390, 35.2517),
            feature({"Name": "Main Library", "Address": "310 N Tryon St"}, -80.8395, 35.2220),
            feature({"Name": "Mid Branch", "Address": "1 Mid St"}, -80.8390, 35.2317),
        ],
    )

    data = structured(
        await call(
            "find_nearby_places",
            {"category": "library", "location": DOWNTOWN, "limit": 2, "radius_miles": 3},
        )
    )

    assert data["total_within_radius"] == 3
    assert [place["name"] for place in data["places"]] == ["Main Library", "Mid Branch"]
    assert data["places"][0]["source"] == "Libraries"
    assert "Name" not in data["places"][0]
    assert data["places"][0]["distance_miles"] <= data["places"][1]["distance_miles"]


async def test_greenway_segments_collapse_to_the_nearest_per_trail(
    fake_arcgis: FakeArcGIS,
) -> None:
    layer = catalog.PLACE_LAYERS["greenway"][0]
    segment = {"trail_name": "Little Sugar Creek Greenway", "trl_status": "Existing"}
    serve_features(
        fake_arcgis,
        layer.url,
        [
            {"attributes": segment, "geometry": {"paths": [[[-80.84, 35.25], [-80.84, 35.26]]]}},
            {"attributes": segment, "geometry": {"paths": [[[-80.84, 35.22], [-80.84, 35.23]]]}},
        ],
    )
    data = structured(
        await call("find_nearby_places", {"category": "greenway", "location": DOWNTOWN})
    )
    assert data["total_within_radius"] == 1
    assert data["places"][0]["distance_miles"] < 0.2


async def test_query_dataset_refuses_hosts_outside_the_portal(fake_arcgis: FakeArcGIS) -> None:
    result = await call(
        "query_dataset", {"layer_url": "https://example.com/arcgis/rest/services/X/MapServer/0"}
    )
    assert "gis.charlottenc.gov" in error_text(result)
    assert not fake_arcgis.router.calls


async def test_query_dataset_rejects_unsafe_field_names(fake_arcgis: FakeArcGIS) -> None:
    result = await call(
        "query_dataset",
        {"layer_url": catalog.CMPD_INCIDENTS, "fields": ["ZIP", "1; DROP TABLE"]},
    )
    assert "not a valid field name" in error_text(result)


async def test_location_without_house_number_is_a_helpful_error(
    fake_arcgis: FakeArcGIS,
) -> None:
    result = await call("get_crime_near", {"location": "Tryon Street"})
    assert "house number" in error_text(result)


async def test_coordinates_outside_county_are_rejected(fake_arcgis: FakeArcGIS) -> None:
    result = await call("get_address_profile", {"location": "35.7796,-78.6382"})
    assert "outside Mecklenburg County" in error_text(result)


async def test_upstream_outage_is_reported_as_tool_error(fake_arcgis: FakeArcGIS) -> None:
    fake_arcgis.serve(catalog.SOLID_WASTE_ROUTES, lambda _request: httpx.Response(502))
    result = await call("get_trash_and_recycling_schedule", {"location": DOWNTOWN})
    assert "HTTP 502" in error_text(result)


async def test_search_datasets_maps_hub_records(fake_arcgis: FakeArcGIS) -> None:
    hub_record = {
        "id": "511196d07743406983cfe2bb0cd07e57",
        "properties": {
            "title": "Solid Waste Collection",
            "type": "Feature Service",
            "snippet": "Collection routes",
            "url": catalog.SOLID_WASTE_ROUTES,
            "modified": 1733325715000,
            "tags": ["Solid Waste", "Recycling"],
        },
    }

    def hub(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "recycling"
        return httpx.Response(200, json={"numberMatched": 1, "features": [hub_record]})

    fake_arcgis.serve(HUB_SEARCH_URL, hub)
    data = structured(await call("search_datasets", {"query": "recycling"}))
    assert data["total_matching"] == 1
    dataset = data["datasets"][0]
    assert dataset["layer_url"] == catalog.SOLID_WASTE_ROUTES
    assert dataset["portal_page"].endswith("/datasets/511196d07743406983cfe2bb0cd07e57")
    assert dataset["last_modified"] == "2024-12-04T10:21:55-05:00"


async def test_summarize_dataset_needs_a_field_to_sum(fake_arcgis: FakeArcGIS) -> None:
    result = await call(
        "summarize_dataset",
        {"layer_url": catalog.BUDGET, "group_by": ["Fund_Name"], "statistic_type": "sum"},
    )
    assert "statistic_field is required" in error_text(result)


def _budget(request: httpx.Request) -> httpx.Response:
    params = request.url.params
    group_by = params.get("groupByFieldsForStatistics")
    rows: list[dict[str, Any]]
    if group_by == "Fiscal_Year":
        rows = [{"Fiscal_Year": "FY2023", "record_count": 9}, {"Fiscal_Year": "FY2022"}]
    elif group_by == "Department_Name":
        rows = [{"Department_Name": "Police", "total_amount": 123.456}]
    elif "Amount > 0" in params["where"]:
        rows = [{"total_amount": 1500.0}]
    else:
        rows = [{"total_amount": 1000.0}]
    return arcgis_response(request, statistics=rows)


async def test_city_budget_defaults_to_latest_year(fake_arcgis: FakeArcGIS) -> None:
    fake_arcgis.serve(catalog.BUDGET, _budget)
    data = structured(await call("get_city_budget", {"department": "police"}))
    assert data["fiscal_year"] == "FY2023"
    assert data["available_fiscal_years"] == ["FY2023", "FY2022"]
    assert data["rows"] == [{"name": "Police", "amount": 123.46}]
    assert data["net_total_amount"] == 1000.0
    assert data["positive_total_amount"] == 1500.0


async def test_city_budget_rejects_unpublished_year(fake_arcgis: FakeArcGIS) -> None:
    fake_arcgis.serve(catalog.BUDGET, _budget)
    result = await call("get_city_budget", {"fiscal_year": "FY2030"})
    assert "available: FY2023, FY2022" in error_text(result)


async def test_salary_stats_use_latest_quarter_and_filters(fake_arcgis: FakeArcGIS) -> None:
    def salaries(request: httpx.Request) -> httpx.Response:
        group_by = request.url.params.get("groupByFieldsForStatistics")
        if group_by == "Year,Quarter":
            rows: list[dict[str, Any]] = [{"Year": 2026, "Quarter": 3, "record_count": 10}]
        elif group_by == "Dept":
            rows = [{"Dept": "Fire", "employees": 12, "average_rate": 70000.123,
                     "minimum_rate": 40000, "maximum_rate": 200000}]  # fmt: skip
        else:
            rows = [{"employees": 12, "average_rate": 70000.123}]
        return arcgis_response(request, statistics=rows)

    fake_arcgis.serve(catalog.SALARIES, salaries)
    data = structured(await call("get_city_salary_stats", {"department": "fire"}))
    assert (data["year"], data["quarter"]) == (2026, 3)
    assert "UPPER(Dept) LIKE '%FIRE%'" in data["filters"]
    assert data["rows"][0] == {
        "group": "Fire", "employees": 12, "average_annual_rate": 70000.12,
        "minimum_annual_rate": 40000.0, "maximum_annual_rate": 200000.0,
    }  # fmt: skip
    assert data["overall"]["employees"] == 12
