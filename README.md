# Esri UC 2026 MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

An MCP server for the **2026 Esri User Conference** (San Diego, July 13–17):
session catalog, expo directory, time-aware schedule ("what's on right now"),
schedule-block planning with conflict detection, and room locations — built on
the [OpenContext](docs/ARCHITECTURE.md) one-fork-one-server framework.

**Endpoint:** `https://esri-uc.codeforanchorage.org/mcp`

## Provenance

All data is a **static snapshot of the RainFocus public session catalog**
(`event.esri.com`, widget `esri/26uc` — the same API that powers the public
[detailed agenda](https://registration.esri.com/flow/esri/26uc/eventportal/page/detailed-agenda)),
taken **2026-07-14 UTC** with `scripts/snapshot.py`: 982 sessions, 321
exhibitors, and 223 room geometries from the official
[UC Event Map's](https://webapps-cdn.esri.com/CDN/uc-event-maps/web.html)
backing ArcGIS service (the conference venue is itself a feature service).
Nothing behind attendee login is accessed — no My Schedule, reservations, or
waitlists. The server makes **zero upstream calls at runtime**; every response
carries the snapshot date, and a catalog refresh is simply
`python scripts/snapshot.py` + redeploy. Room changes happen — attendees
should verify last-minute changes in the Esri Events app.

## Try these prompts

1. *"What MCP or agentic AI sessions are on Wednesday afternoon, and do any
   of them conflict?"*
2. *"What's starting in the next hour that's relevant to local government?"*
3. *"Where is my next session, and is there coffee within a five-minute walk
   of that room?"* (composes `where_is_room`'s lat/lng with maps tools)

## Tools

| Tool | Answers |
| --- | --- |
| `search_sessions` | keyword + day/type/track/time-window search |
| `get_session` | full record: abstract, speakers, room, livestream |
| `whats_on` | in progress now / starting soon, venue-local clock |
| `plan_block` | candidates for a time block, overlap conflicts marked |
| `find_exhibitors` | expo directory with booth numbers |
| `get_tracks_and_types` | exact filter vocabulary (types, tracks, days…) |
| `where_is_room` | building, floor, lat/lng, event-map deep link |

## Quick start (local)

```bash
cp config-example.yaml config.yaml   # esri_uc is already enabled in config.yaml
python3 scripts/local_server.py      # http://localhost:8000/mcp
```

Refresh the snapshot (public catalog only; sequential, 1 s delay between
requests): `python scripts/snapshot.py`, review the diff in `data/`, redeploy
with `./scripts/deploy.sh --environment prod`.

Connect via **Claude Connectors**: Settings → Connectors → Add custom
connector → paste the endpoint URL.

---

## Framework documentation

| Doc                                        | Description                                     |
| ------------------------------------------ | ----------------------------------------------- |
| [Getting Started](docs/GETTING_STARTED.md) | Setup and usage                                 |
| [Architecture](docs/ARCHITECTURE.md)       | System design and plugins                       |
| [Deployment](docs/DEPLOYMENT.md)           | AWS, Terraform, monitoring                      |
| [Testing](docs/TESTING.md)                 | Local testing (Terminal, Claude, MCP Inspector) |

Built on OpenContext by Srihari Raman (City of Boston DoIT); this fork by
Code for Anchorage. MIT licensed — see [LICENSE](LICENSE).
