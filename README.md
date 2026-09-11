# QueensEstate

An [MCP](https://modelcontextprotocol.io) server that lets an LLM answer Charlotte residents'
everyday questions with official public data from the
**City of Charlotte Open Data Portal** — <https://data.charlottenc.gov>.

> The portal lives at `data.charlottenc.gov`.
> It is an ArcGIS Hub site; its ~380 datasets are ArcGIS REST layers run by the City of
> Charlotte and Mecklenburg County. No API key is required.

## What's on the portal

A survey of the catalog (`/api/feed/dcat-us/1.1.json`) shows these main themes:

| Theme | Examples |
| --- | --- |
| Public safety | CMPD incidents (870k+ since 2017, updated daily), homicides, traffic stops, fire incident reports, crashes |
| City services | 311 service requests (3.4M), solid-waste collection routes, code enforcement cases, street closures |
| Planning & zoning | Zoning, rezoning petitions, historic districts, 2040 policy map, floodplains, parcels |
| Transportation | CATS bus routes and stops, LYNX light rail, park-and-ride, bike lanes, sidewalks, greenways |
| Places | Libraries, schools, parks, fire/police stations, pharmacies, grocery stores, EV chargers, day care |
| Government | Capital projects, budget, employee salaries, council and commissioner districts |
| Demographics | Census block groups/tracts, Quality of Life neighborhood profiles |

## Tools

The tools are chosen for the questions residents actually ask. Every tool is read-only, and
every location parameter accepts either an address (`"600 E 4th St, Charlotte"`) or
`"latitude,longitude"`.

### My address

| Tool | Answers questions like |
| --- | --- |
| `get_address_profile` | "Who is my council member?" "What police division and fire station serve me?" "What's my zoning? Am I in a flood zone or historic district?" |
| `get_trash_and_recycling_schedule` | "What day is trash pickup? Is this a recycling week (GREEN/ORANGE)?" |
| `lookup_address` | "Is this a valid address? What's its parcel ID and municipality?" |

### Safety

| Tool | Answers questions like |
| --- | --- |
| `get_crime_near` | "Any car break-ins near my apartment this month?" |
| `summarize_crime` | "How have robberies in Charlotte changed since 2019?" "Which division has the most burglaries?" |
| `get_traffic_crashes_near` | "Is the intersection by my kid's school dangerous?" |

### City services and neighborhood change

| Tool | Answers questions like |
| --- | --- |
| `get_311_requests_near` | "Has anyone already reported this pothole or streetlight?" |
| `get_code_enforcement_cases` | "Does the house I'm renting have open housing-code violations?" |
| `get_street_closures` | "Are any roads closed on my commute?" |
| `get_capital_projects_near` | "What's being built near me, when is it done, and who do I contact?" |
| `get_pending_rezonings` | "Is anyone trying to rezone land near my neighborhood?" |

### Places and transit

| Tool | Answers questions like |
| --- | --- |
| `find_nearby_places` | Nearest library, school, park, greenway, pharmacy, grocery store, EV charger, light rail station, bus stop, public Wi-Fi… (18 categories) |
| `get_bus_route` | "How often does the 9 run on Sunday evenings?" |

### Government transparency

| Tool | Answers questions like |
| --- | --- |
| `get_city_budget` | "How much does the city budget for Police?" |
| `get_city_salary_stats` | "What do Charlotte firefighters earn on average?" (aggregates only, no names) |

### Anything else in the catalog

| Tool | Purpose |
| --- | --- |
| `search_datasets` | Keyword search over the whole portal |
| `describe_dataset` | Fields, types, and record count for a layer |
| `query_dataset` | SQL-filtered records from any layer, optionally near a location |
| `summarize_dataset` | Server-side counts/sums grouped by fields |

## Setup

Requires Python 3.14 (developed on 3.14.7).

```bash
~/.pyenv/versions/3.14.7/bin/python -m venv .venv
.venv/bin/python -m pip install -e . --group dev
```

### Use with Claude Code

```bash
claude mcp add queensestate -- /absolute/path/to/queensestate/.venv/bin/queensestate
```

### Use with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "queensestate": {
      "command": "/absolute/path/to/queensestate/.venv/bin/queensestate"
    }
  }
}
```

### Other transports and debugging

```bash
.venv/bin/queensestate --transport streamable-http --port 8000   # http://127.0.0.1:8000/mcp
npx @modelcontextprotocol/inspector .venv/bin/queensestate       # interactive inspector
```

## Development

```bash
.venv/bin/pytest                 # unit + in-memory MCP tests; no network access
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/mypy
```

Layout:

- `src/queensestate/arcgis.py`: async ArcGIS REST / Hub client (timeouts, bounded retries, host allowlist)
- `src/queensestate/address.py`: address matching against the county Master Address layer
- `src/queensestate/catalog.py`: the curated layers behind the purpose-built tools
- `src/queensestate/server.py`: the MCP tools

## Data notes and limitations

- **Geocoding** uses Mecklenburg County's Master Address layer, so only addresses inside the
  county resolve. Coordinates outside the county are rejected.
- **Freshness varies by dataset**: CMPD incidents, 311 requests, and code enforcement are current
  within days; the Budget Report dataset currently runs FY2018–FY2023. Tools report what the
  portal publishes.
- **CMPD locations are generalized** by the department for privacy.
- **311 data** records when requests were received, not when they were resolved.
- **Timestamps** are ISO 8601 in Charlotte local time with the UTC offset
  (e.g. `2026-09-06T00:00:00-04:00`). The underlying services store UTC.
- **Budget amounts** include large negative lines (e.g. "00 Non Department", Charlotte Water),
  so `get_city_budget` reports both the net total and the sum of positive lines.
- **Statistics limits**: some city map services ignore `resultRecordCount` on aggregate
  queries, so the client enforces row limits itself.
- **Live bus arrivals** are not on the portal; pair this server with a CATS real-time source.
  Bus stop IDs from `find_nearby_places` match CATS stop IDs.
- The generic tools only reach hosts that serve portal datasets (`gis.charlottenc.gov`,
  `meckgis.mecklenburgcountync.gov`, `services.arcgis.com`, `gis.ci.charlotte.nc.us`).
- Mecklenburg County's server rejects default Python user agents, so the client sends its own.

## Ideas for more tools

- Neighborhood Quality of Life indicators by NPA (income, age, housing, amenities)
- Charlotte Fire incident reports by address block
- CMPD officer traffic-stop statistics
- Sidewalk and bike network gaps near an address
- Tree canopy and land surface temperature by neighborhood
