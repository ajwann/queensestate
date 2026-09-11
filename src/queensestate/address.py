"""Resolve what a resident types ("600 E 4th St, Charlotte") to a point on the map.

Addresses are matched against Mecklenburg County's Master Address layer, the authoritative
list of addressable locations in the county, so no third-party geocoder is involved.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from queensestate import catalog
from queensestate.arcgis import ArcGISClient, Feature, sql_quote
from queensestate.geo import Point, parse_coordinates

_DIRECTIONS: Final = {
    "N": "N", "NORTH": "N", "S": "S", "SOUTH": "S", "E": "E", "EAST": "E", "W": "W", "WEST": "W",
    "NE": "NE", "NORTHEAST": "NE", "NW": "NW", "NORTHWEST": "NW",
    "SE": "SE", "SOUTHEAST": "SE", "SW": "SW", "SOUTHWEST": "SW",
}  # fmt: skip
_ORDINALS: Final = {
    "FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH",
    "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH", "NINTH": "9TH", "TENTH": "10TH",
    "ELEVENTH": "11TH", "TWELFTH": "12TH",
}  # fmt: skip
_ZIP_SUFFIX: Final = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
_UNIT_SUFFIX: Final = re.compile(
    r"\s(?:APT|APARTMENT|UNIT|STE|SUITE|BLDG|LOT|RM|ROOM|#)\s*#?\s*([A-Z0-9-]{1,10})$"
)
_HOUSE_NUMBER: Final = re.compile(r"(\d{1,6})[A-Z]?\s+(.+)")
_NON_ADDRESS_CHARS: Final = re.compile(r"[^A-Z0-9#&' -]+")
_MAX_STREET_WORDS: Final = 8
_ADDRESS_FIELDS: Final = (
    "FullAddress", "Direction", "StreetName", "StreetType", "Unit", "ZipCode", "PostalCity",
    "Jurisdiction", "TaxParcelID",
)  # fmt: skip


class LocationError(ValueError):
    """A location could not be understood, was not found, or lies outside the county."""


@dataclass(frozen=True, slots=True)
class ParsedAddress:
    """The parts of a free-text street address needed to search the Master Address layer.

    ``words`` holds everything typed after the house number, e.g. ``("E", "4TH", "ST")``.
    """

    house_number: int
    words: tuple[str, ...]
    unit: str | None = None
    zip_code: str | None = None

    @property
    def direction(self) -> str | None:
        """The normalized leading directional ("E"), if the first word is one."""
        return _DIRECTIONS.get(self.words[0]) if len(self.words) > 1 else None

    def street_name_candidates(self) -> list[str]:
        """Every leading run of street words, with and without a leading directional.

        The county stores the bare street name ("4TH"), which is always one of these
        prefixes, so trailing street types, cities, states, or ZIP codes do no harm.
        """
        word_lists = [self.words[1:], self.words] if self.direction else [self.words]
        prefixes = (
            " ".join(words[:length])
            for words in word_lists
            for length in range(1, min(len(words), _MAX_STREET_WORDS) + 1)
        )
        return list(dict.fromkeys(prefixes))

    def word_after(self, street_name: str) -> str | None:
        """Return the word typed right after ``street_name`` (usually the street type)."""
        name_words = tuple(street_name.split())
        for start in (0, 1):
            if self.words[start : start + len(name_words)] == name_words:
                following = self.words[start + len(name_words) :]
                return following[0] if following else None
        return None


@dataclass(frozen=True, slots=True)
class AddressMatch:
    """An address record from the county's Master Address layer."""

    full_address: str
    point: Point
    parcel_id: str | None
    jurisdiction: str | None
    postal_city: str | None
    zip_code: str | None


@dataclass(frozen=True, slots=True)
class ResolvedLocation:
    """A user-supplied location resolved to a point, with the matched address if any."""

    query: str
    point: Point
    address: AddressMatch | None = None


def parse_address(text: str) -> ParsedAddress:
    """Split a free-text address into house number, street words, unit, and ZIP code.

    Raises:
        LocationError: If there is no leading house number or no street name.
    """
    upper = text.upper().replace(".", "")
    zip_match = _ZIP_SUFFIX.search(upper)
    street_line = " ".join(_NON_ADDRESS_CHARS.sub(" ", upper.split(",", 1)[0]).split())
    unit = None
    if unit_match := _UNIT_SUFFIX.search(street_line):
        unit = unit_match[1]
        street_line = street_line[: unit_match.start()]
    house = _HOUSE_NUMBER.fullmatch(street_line)
    words = (
        tuple(_ORDINALS.get(word, word) for word in house[2].replace("#", " ").split())
        if house
        else ()
    )
    if house is None or not words:
        raise LocationError(
            "Include a house number and street, for example '600 E 4th St', "
            "or give coordinates as 'latitude,longitude'."
        )
    return ParsedAddress(
        house_number=int(house[1]),
        words=words,
        unit=unit,
        zip_code=zip_match[1] if zip_match else None,
    )


async def geocode(client: ArcGISClient, text: str, *, max_matches: int = 5) -> list[AddressMatch]:
    """Return the best Master Address matches for ``text``, most likely first.

    Returns an empty list when nothing matches.

    Raises:
        LocationError: If ``text`` is not a parseable street address.
        ArcGISError: If the address service fails.
    """
    parsed = parse_address(text)
    street_names = ", ".join(sql_quote(name) for name in parsed.street_name_candidates())
    result = await client.query(
        catalog.MASTER_ADDRESS,
        where=f"HouseNumber = {parsed.house_number} AND StreetName IN ({street_names})",
        out_fields=_ADDRESS_FIELDS,
        order_by="Unit",
        limit=200,
        return_geometry=True,
    )
    ranked = sorted(result.features, key=lambda f: _score(parsed, f.attributes), reverse=True)
    matches: dict[str, AddressMatch] = {}
    for feature in ranked:
        match = _to_match(feature)
        if match is not None:
            matches.setdefault(match.full_address, match)
        if len(matches) == max_matches:
            break
    return list(matches.values())


async def resolve_location(client: ArcGISClient, text: str) -> ResolvedLocation:
    """Resolve a street address or ``"latitude,longitude"`` string inside Mecklenburg County.

    Raises:
        LocationError: If the text cannot be parsed, no address matches, or the point is
            outside the county.
        ArcGISError: If the address service fails.
    """
    text = text.strip()
    if (point := parse_coordinates(text)) is not None:
        if not point.in_service_area():
            raise LocationError(
                f"{point.latitude},{point.longitude} is outside Mecklenburg County; "
                "this server only covers Charlotte-Mecklenburg."
            )
        return ResolvedLocation(query=text, point=point)
    matches = await geocode(client, text, max_matches=1)
    if not matches:
        raise LocationError(
            f"No address matching {text!r} was found in Mecklenburg County's master address "
            "list. Check the house number and street name, or pass 'latitude,longitude'."
        )
    return ResolvedLocation(query=text, point=matches[0].point, address=matches[0])


def _score(parsed: ParsedAddress, attributes: Mapping[str, Any]) -> int:
    street_name = str(attributes.get("StreetName", ""))
    score = 10 * len(street_name.split())  # prefer the most specific street-name match
    direction = attributes.get("Direction")
    if parsed.direction and direction:
        score += 4 if direction == parsed.direction else -4
    elif not parsed.direction and not direction:
        score += 1
    typed_type = parsed.word_after(street_name)
    if typed_type and _is_abbreviation(attributes.get("StreetType"), typed_type):
        score += 3
    if parsed.zip_code and attributes.get("ZipCode") == parsed.zip_code:
        score += 3
    unit = attributes.get("Unit")
    if parsed.unit:
        score += 3 if unit == parsed.unit else 0
    elif not unit:
        score += 2
    return score


def _is_abbreviation(short: object, word: str) -> bool:
    """Return whether ``short`` abbreviates ``word`` by dropping letters ("RD" for "ROAD")."""
    if not isinstance(short, str) or not short or short[0] != word[0]:
        return False
    letters = iter(word)
    return all(letter in letters for letter in short)


def _to_match(feature: Feature) -> AddressMatch | None:
    geometry = feature.geometry or {}
    x, y = geometry.get("x"), geometry.get("y")
    attributes = feature.attributes
    full_address = attributes.get("FullAddress")
    if not full_address or not isinstance(x, int | float) or not isinstance(y, int | float):
        return None
    return AddressMatch(
        full_address=str(full_address),
        point=Point(latitude=float(y), longitude=float(x)),
        parcel_id=_text(attributes.get("TaxParcelID")),
        jurisdiction=_text(attributes.get("Jurisdiction")),
        postal_city=_text(attributes.get("PostalCity")),
        zip_code=_text(attributes.get("ZipCode")),
    )


def _text(value: object) -> str | None:
    return None if value is None else str(value)
