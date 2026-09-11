import httpx
import pytest
from conftest import FakeArcGIS, arcgis_response, feature

from queensestate import catalog
from queensestate.address import (
    LocationError,
    ParsedAddress,
    _is_abbreviation,
    geocode,
    parse_address,
    resolve_location,
)
from queensestate.arcgis import ArcGISClient


def test_parse_address_with_city_state_and_zip() -> None:
    parsed = parse_address("600 E. 4th St, Charlotte, NC 28202")
    assert parsed == ParsedAddress(house_number=600, words=("E", "4TH", "ST"), zip_code="28202")
    assert parsed.direction == "E"


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("600 East Fourth Street Apt 4000", ("EAST", "4TH", "STREET")),
        ("600 E 4th St #4000", ("E", "4TH", "ST")),
        ("600 e 4th st unit 4000", ("E", "4TH", "ST")),
    ],
)
def test_parse_address_extracts_unit(text: str, words: tuple[str, ...]) -> None:
    parsed = parse_address(text)
    assert parsed.words == words
    assert parsed.unit == "4000"
    assert parsed.direction == "E"


@pytest.mark.parametrize("text", ["Main Street", "600", "600 #", "   "])
def test_parse_address_requires_house_number_and_street(text: str) -> None:
    with pytest.raises(LocationError, match="house number"):
        parse_address(text)


def test_street_name_candidates_cover_prefixes_with_and_without_direction() -> None:
    parsed = ParsedAddress(house_number=600, words=("E", "4TH", "ST", "CHARLOTTE"))
    assert parsed.street_name_candidates() == [
        "4TH", "4TH ST", "4TH ST CHARLOTTE", "E", "E 4TH", "E 4TH ST", "E 4TH ST CHARLOTTE",
    ]  # fmt: skip
    assert parsed.word_after("4TH") == "ST"
    assert parsed.word_after("E 4TH") == "ST"
    assert parsed.word_after("CHARLOTTE") is None


def test_a_bare_street_name_is_never_mistaken_for_a_direction() -> None:
    assert ParsedAddress(house_number=1, words=("NORTH",)).direction is None


@pytest.mark.parametrize(
    ("short", "word", "expected"),
    [("RD", "ROAD", True), ("AV", "AVE", True), ("TL", "TRAIL", True), ("ST", "ST", True),
     ("ST", "DRIVE", False), ("DR", "ROAD", False), (None, "ST", False), ("", "ST", False)],
)  # fmt: skip
def test_is_abbreviation(short: str | None, word: str, expected: bool) -> None:
    assert _is_abbreviation(short, word) is expected


def _master_address(full: str, direction: str | None, unit: str | None = None) -> dict[str, object]:
    attributes = {
        "FullAddress": full,
        "Direction": direction,
        "StreetName": "4TH",
        "StreetType": "ST",
        "Unit": unit,
        "ZipCode": "28202",
        "Jurisdiction": "CHARLOTTE",
        "TaxParcelID": "12502601",
    }
    return feature(attributes, longitude=-80.8396, latitude=35.2212)


@pytest.mark.anyio
async def test_geocode_ranks_base_address_with_matching_direction_first(
    fake_arcgis: FakeArcGIS,
) -> None:
    records = [
        _master_address("600 E 4TH ST 4000 CHARLOTTE NC 28202", "E", unit="4000"),
        _master_address("600 W 4TH ST CHARLOTTE NC 28202", "W"),
        _master_address("600 E 4TH ST CHARLOTTE NC 28202", "E"),
    ]
    fake_arcgis.serve(catalog.MASTER_ADDRESS, lambda request: arcgis_response(request, records))
    async with httpx.AsyncClient() as http:
        matches = await geocode(ArcGISClient(http), "600 East 4th Street", max_matches=2)

    assert [m.full_address for m in matches] == [
        "600 E 4TH ST CHARLOTTE NC 28202",
        "600 E 4TH ST 4000 CHARLOTTE NC 28202",
    ]
    assert matches[0].parcel_id == "12502601"
    assert matches[0].point.latitude == pytest.approx(35.2212)
    where = fake_arcgis.requests_to(catalog.MASTER_ADDRESS)[0].url.params["where"]
    assert where.startswith("HouseNumber = 600 AND StreetName IN (")
    assert "'4TH'" in where


@pytest.mark.anyio
async def test_resolve_location_accepts_coordinates_without_a_lookup(
    fake_arcgis: FakeArcGIS,
) -> None:
    async with httpx.AsyncClient() as http:
        resolved = await resolve_location(ArcGISClient(http), "35.2217,-80.8390")
    assert resolved.point.latitude == pytest.approx(35.2217)
    assert resolved.address is None
    assert not fake_arcgis.router.calls


@pytest.mark.anyio
async def test_resolve_location_rejects_points_outside_the_county(
    fake_arcgis: FakeArcGIS,
) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(LocationError, match="outside Mecklenburg County"):
            await resolve_location(ArcGISClient(http), "35.7796,-78.6382")


@pytest.mark.anyio
async def test_resolve_location_reports_unknown_addresses(fake_arcgis: FakeArcGIS) -> None:
    fake_arcgis.serve(catalog.MASTER_ADDRESS, lambda request: arcgis_response(request, []))
    async with httpx.AsyncClient() as http:
        with pytest.raises(LocationError, match="No address matching"):
            await resolve_location(ArcGISClient(http), "99999 Nowhere Ln")
