"""Curated layers from data.charlottenc.gov used by the purpose-built tools.

Each URL is the "ArcGIS GeoService" distribution of a dataset listed in the portal's DCAT
catalog (https://data.charlottenc.gov/api/feed/dcat-us/1.1.json).
"""

from dataclasses import dataclass
from typing import Final, Literal

_CITY: Final = "https://gis.charlottenc.gov/arcgis/rest/services"
_AGOL: Final = "https://services.arcgis.com/9Nl857LBlQVyzq54/arcgis/rest/services"
_COUNTY: Final = "https://meckgis.mecklenburgcountync.gov/server/rest/services"

# Addresses, boundaries, and districts
MASTER_ADDRESS: Final = f"{_CITY}/CountyData/MasterAddress/MapServer/0"
COUNCIL_DISTRICTS: Final = f"{_CITY}/PLN/CouncilDistricts/MapServer/0"
COMMISSIONER_DISTRICTS: Final = f"{_COUNTY}/MecklenburgCountyCommissionerDistricts/MapServer/0"
POLICE_DIVISIONS: Final = f"{_AGOL}/CMPD_Police_Divisions/FeatureServer/0"
FIRE_STATION_AREAS: Final = f"{_AGOL}/Fire_Station_Administrative_Areas/FeatureServer/0"
FIRE_STATIONS: Final = f"{_AGOL}/Current_CFD_Fire_Stations/FeatureServer/0"
SOLID_WASTE_ROUTES: Final = f"{_AGOL}/Solid_Waste_Collection/FeatureServer/0"
ZONING: Final = f"{_CITY}/PLN/Zoning/MapServer/0"
HISTORIC_DISTRICTS: Final = f"{_AGOL}/Historic_Districts/FeatureServer/0"
FEMA_FLOODPLAIN: Final = f"{_COUNTY}/FEMAFloodplain/FeatureServer/0"
ZIP_CODES: Final = f"{_COUNTY}/ZipCodeBoundaries/FeatureServer/0"

# Public safety
CMPD_INCIDENTS: Final = f"{_CITY}/CMPD/CMPDIncidents/MapServer/0"
CRASHES: Final = f"{_CITY}/CDOT/TCLS_Crashes/MapServer/0"

# City services and development
SERVICE_REQUESTS_311: Final = f"{_CITY}/ODP/ServiceRequests311/MapServer/0"
CODE_ENFORCEMENT: Final = f"{_CITY}/HNS/CodeEnforcementCasesAll/MapServer/0"
STREET_CLOSURES: Final = f"{_CITY}/CDOT/StreetClosuresAndDetours/MapServer/0"
CAPITAL_PROJECT_POINTS: Final = f"{_CITY}/CIP/Capital_Investment_Projects_Points/MapServer/0"
CAPITAL_PROJECT_AREAS: Final = f"{_CITY}/CIP/Capital_Improvements_Projects_Polygons/MapServer/0"
REZONINGS: Final = f"{_CITY}/PLN/Rezonings/MapServer/0"
BUS_ROUTES: Final = f"{_AGOL}/Bus_Routes/FeatureServer/0"

# Government finance
BUDGET: Final = f"{_CITY}/ODP/BudgetReport/MapServer/1"
SALARIES: Final = f"{_CITY}/ODP/CityofCharlotteEmployeeSalaries/MapServer/1"


PlaceCategory = Literal[
    "library",
    "public_school",
    "park",
    "greenway",
    "fire_station",
    "police_station",
    "post_office",
    "pharmacy",
    "grocery_store",
    "medical_facility",
    "child_care",
    "ev_charging",
    "park_and_ride",
    "light_rail_station",
    "bus_stop",
    "ymca",
    "place_of_worship",
    "public_wifi",
]


@dataclass(frozen=True, slots=True)
class PlaceLayer:
    """A layer that answers "what is the nearest <category>?"."""

    source: str
    url: str
    name_field: str
    fields: tuple[str, ...]
    where: str = "1=1"
    one_result_per_name: bool = False
    """Collapse features sharing a name (e.g. the many segments of one greenway)."""


_HOUSING_LOCATIONAL = f"{_CITY}/HNS/HousingLocationalToolLayers/MapServer"
_CURRENT = "DateDeleted IS NULL"

PLACE_LAYERS: Final[dict[PlaceCategory, tuple[PlaceLayer, ...]]] = {
    "library": (
        PlaceLayer(
            "Libraries",
            f"{_AGOL}/Libraries/FeatureServer/0",
            "Name",
            ("Name", "Address", "City", "Zip", "Status"),
        ),
    ),
    "public_school": (
        PlaceLayer(
            "CMS public schools",
            f"{_COUNTY}/CMSPublicSchool/FeatureServer/0",
            "school",
            (
                "school",
                "school_typ",
                "grdlevl",
                "address",
                "city",
                "zipcode",
                "magnet",
                "mag_focus",
                "schl_stat",
            ),
        ),
    ),
    "park": (
        PlaceLayer(
            "Parks",
            f"{_HOUSING_LOCATIONAL}/10",
            "PRKNAME",
            ("PRKNAME", "PRKADDR", "CITY", "ZIP", "PRKTYPE", "PRKSIZE", "PRKSTATUS"),
        ),
    ),
    "greenway": (
        PlaceLayer(
            "Greenways",
            f"{_COUNTY}/GreenwayTrails/FeatureServer/0",
            "trail_name",
            ("trail_name", "trl_status", "trail_surf", "ada_comp"),
            one_result_per_name=True,
        ),
    ),
    "fire_station": (
        PlaceLayer("Current CFD fire stations", FIRE_STATIONS, "NAME", ("NAME", "NUM", "ADDRESS")),
    ),
    "police_station": (
        PlaceLayer(
            "CMPD police division offices",
            f"{_AGOL}/CMPD_Police_Division_Office/FeatureServer/0",
            "NAME",
            ("NAME", "ADDRESS", "City", "Zip"),
        ),
    ),
    "post_office": (
        PlaceLayer(
            "Post offices",
            f"{_AGOL}/Post_Office/FeatureServer/0",
            "Name",
            ("Name", "Facility", "Address", "City", "Zip"),
        ),
    ),
    "pharmacy": (
        PlaceLayer(
            "Pharmacies", f"{_HOUSING_LOCATIONAL}/2", "Name", ("Name", "Address"), where=_CURRENT
        ),
    ),
    "grocery_store": (
        PlaceLayer(
            "Grocery stores",
            f"{_HOUSING_LOCATIONAL}/6",
            "Name",
            ("Name", "Address"),
            where=_CURRENT,
        ),
    ),
    "medical_facility": (
        PlaceLayer(
            "Medical facilities",
            f"{_HOUSING_LOCATIONAL}/4",
            "Name",
            ("Name", "Address", "ServiceType"),
            where=_CURRENT,
        ),
    ),
    "child_care": (
        PlaceLayer(
            "Day care facilities",
            f"{_CITY}/PLN/DayCare/MapServer/0",
            "FacilityName",
            ("FacilityName", "ADDRESS", "City", "Zip", "PHONE", "LicenseType"),
        ),
    ),
    "ev_charging": (
        PlaceLayer(
            "Electric vehicle charging stations",
            f"{_CITY}/IT/ElectricalChargingStations/MapServer/0",
            "Station_Name",
            (
                "Station_Name",
                "Street_Address",
                "Facility_Type",
                "EV_Network",
                "EV_Level2_EVSE_Num",
                "EV_Pricing",
                "Groups_With_Access_Code",
            ),
        ),
    ),
    "park_and_ride": (
        PlaceLayer(
            "CATS park and ride lots",
            f"{_AGOL}/CATS_Park_and_Ride_Lots/FeatureServer/0",
            "Name",
            ("Name", "Street", "City", "Spaces", "Type", "Status"),
        ),
    ),
    "light_rail_station": (
        PlaceLayer(
            "LYNX Blue Line stations",
            f"{_AGOL}/LYNX_Blue_Line_Stations/FeatureServer/0",
            "NAME",
            ("NAME", "Address", "ParknRide", "ParkSpaces", "StationTyp"),
        ),
        PlaceLayer(
            "CityLYNX Gold Line stops",
            f"{_AGOL}/LYNX_Gold_Line_Stops/FeatureServer/0",
            "Stop_Name",
            ("Stop_Name", "Address", "Status"),
        ),
    ),
    "bus_stop": (
        PlaceLayer(
            "CATS bus stops",
            f"{_AGOL}/Bus_Stops/FeatureServer/0",
            "StopDesc",
            ("StopID", "StopDesc", "routes", "Direction", "Shelter", "Bench"),
        ),
    ),
    "ymca": (
        PlaceLayer(
            "YMCA", f"{_AGOL}/YMCA/FeatureServer/0", "Name", ("Name", "Address", "City", "Zip")
        ),
    ),
    "place_of_worship": (
        PlaceLayer(
            "Places of worship",
            f"{_AGOL}/Places_of_Worship/FeatureServer/0",
            "Name",
            ("Name", "Address", "City", "Zip", "Website"),
        ),
    ),
    "public_wifi": (
        PlaceLayer(
            "Access Charlotte Wi-Fi sites",
            f"{_AGOL}/Access_Charlotte_Wifi_Infrastructure/FeatureServer/0",
            "Name",
            ("Name", "address", "Space_Type"),
        ),
    ),
}
