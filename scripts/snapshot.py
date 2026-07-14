#!/usr/bin/env python3
"""Snapshot the Esri UC 2026 RainFocus catalog into bundled JSON data files.

Replays the public catalog request captured in capture/raw_curl.txt (see that
file for provenance). Fetches ALL sessions (UC + co-located summits) and the
expo exhibitor directory, then normalizes them into the data model served by
the esri_uc plugin:

    data/sessions.json     {"snapshot": {...}, "sessions": [...]}
    data/exhibitors.json   {"snapshot": {...}, "exhibitors": [...]}

A catalog refresh is: run this script, review the diff, redeploy. The MCP
server NEVER calls RainFocus at runtime — it serves these files only.

Rules of engagement honored here:
- Public catalog only; no cookies, no attendee auth (verified: the endpoint
  answers anonymously with just the two public rf* widget header IDs).
- Polite: sequential requests, 50-record pages, 1 second delay between calls.

Usage:  python scripts/snapshot.py
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "capture" / "raw"
DATA_DIR = REPO_ROOT / "data"

# Public widget identifiers captured from the anonymous detailed-agenda page
# (capture/raw_curl.txt). These are NOT secrets — they are served to every
# visitor in the page source and only grant read access to the public catalog.
SESSIONS_URL = "https://event.esri.com/api/sessions"
SEARCH_URL = "https://event.esri.com/api/search"
RF_WIDGET_ID = "gs5nPlJIt3tgPXa967uifi5UUSE4wqyI"
RF_API_PROFILE_ID = "jjfM5Cb5NK6gy9f14WVVwis32oNvv6eb"

PAGE_SIZE = 50
DELAY_SECONDS = 1.0
VENUE_TZ = ZoneInfo("America/Los_Angeles")

# Esri UC Event Map backing service (see capture/event_map_services.txt).
# Layer 3 (Units) carries RF_RoomName — the exact RainFocus room string —
# for every session room; layer 4 (Levels) names the floors.
EVENT_MAP_BASE = (
    "https://esrieventmaps.esrievents.geocloud.com"
    "/arcgis/rest/services/UC_2026_WebMap/MapServer"
)
EVENT_MAP_APP = "https://webapps-cdn.esri.com/CDN/uc-event-maps/web.html"

# Item attributevalues that are workflow/plumbing noise, not user-facing tags.
NOISE_ATTRIBUTES = {
    "MobilePublished",
    "LockMobileReg",
    "AutoPopulate",
    "IncludeSessioninProceedings",
    "ExhibitorCatalog",
    "ExhibitorStatus",
    "BoothNumber",
}


def _post(url: str, form: dict) -> dict:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "rfWidgetId": RF_WIDGET_ID,
            "rfApiProfileId": RF_API_PROFILE_ID,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _extract_page(data: dict) -> tuple:
    """Return (items, total) from either response shape.

    The first page (no `from` param) wraps the section in
    {"sectionList": [{...}]}; paginated pages return the section flat.
    """
    if "sectionList" in data:
        section = data["sectionList"][0]
    else:
        section = data
    return section.get("items", []), int(section.get("total", 0))


def fetch_all(url: str, base_form: dict, kind: str) -> list:
    """Fetch every record for a catalog type, politely, saving raw pages."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    items: list = []
    offset = 0
    total = None
    while True:
        form = dict(base_form)
        if offset:
            form["from"] = offset
            form["size"] = PAGE_SIZE
        else:
            form["size"] = PAGE_SIZE
        data = _post(url, form)
        if str(data.get("responseCode")) not in ("0", "None"):
            raise RuntimeError(
                f"{kind}: responseCode={data.get('responseCode')} "
                f"message={data.get('responseMessage')}"
            )
        page_items, page_total = _extract_page(data)
        raw_path = RAW_DIR / f"{kind}_{offset:05d}.json"
        raw_path.write_text(
            json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        if total is None:
            total = page_total
        items.extend(page_items)
        print(f"  {kind}: {len(items)}/{total} (page at offset {offset})")
        if not page_items or len(items) >= total:
            break
        offset += len(page_items)
        time.sleep(DELAY_SECONDS)
    return items


# ── Normalization (Phase 1 data model) ──────────────────────────────────


def _attr_map(item: dict) -> dict:
    """Group attributevalues into {attribute_id: [values...]}, ordered."""
    out: dict = {}
    for av in item.get("attributevalues", []) or []:
        attr = av.get("attribute_id") or av.get("attribute") or ""
        value = av.get("value")
        if not attr or value in (None, ""):
            continue
        out.setdefault(attr, [])
        if value not in out[attr]:
            out[attr].append(value)
    return out


def _utc_to_venue_iso(utc_str: str) -> str:
    """'2026/07/14 14:00:00' (UTC) -> '2026-07-14T07:00:00-07:00' (venue)."""
    if not utc_str:
        return ""
    dt = datetime.strptime(utc_str, "%Y/%m/%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(VENUE_TZ).isoformat()


def _split_room(room: str) -> tuple:
    """'Ballroom 06 | SDCC' -> ('Ballroom 06', 'SDCC')."""
    if not room:
        return "", ""
    if "|" in room:
        name, _, building = room.rpartition("|")
        return name.strip(), building.strip()
    return room.strip(), ""


def normalize_session(item: dict) -> dict:
    attrs = _attr_map(item)
    occurrences = []
    for t in item.get("times", []) or []:
        room_full = t.get("room", "") or ""
        room, building = _split_room(room_full)
        occurrences.append(
            {
                "day": t.get("dayName", ""),
                "date": t.get("date", ""),
                "start_iso": _utc_to_venue_iso(t.get("utcStartTime", "")),
                "end_iso": _utc_to_venue_iso(t.get("utcEndTime", "")),
                "room": room,
                "venue_building": building,
                "room_map_url": t.get("roomUrl", ""),
            }
        )
    occurrences.sort(key=lambda o: o["start_iso"])

    speakers = [
        {
            "name": p.get("fullName", "").strip(),
            "org": (p.get("companyName") or p.get("globalCompany") or "").strip(),
        }
        for p in item.get("participants", []) or []
        if p.get("fullName")
    ]

    access = attrs.get("AccessType", [])
    livestream = any("live" in a.lower() for a in access)

    raw_tags = {k: v for k, v in attrs.items() if k not in NOISE_ATTRIBUTES}

    return {
        "id": item.get("sessionID", ""),
        "code": item.get("abbreviation") or item.get("code") or "",
        "title": item.get("title", ""),
        "abstract": (item.get("abstract") or "").strip(),
        "type": item.get("type", ""),
        "tracks": attrs.get("Topic", []),
        "event": (attrs.get("Event") or [""])[0],
        "level": (attrs.get("SessionLevel") or [""])[0],
        "occurrences": occurrences,
        "speakers": speakers,
        "livestream": livestream,
        "raw_tags": raw_tags,
    }


def normalize_exhibitor(item: dict) -> dict:
    attrs = _attr_map(item)
    description = re.sub(r"<[^>]+>", "", item.get("description") or "").strip()
    categories: list = []
    for v in attrs.get("ExpoType", []) + attrs.get("RecognitionLevel", []):
        if v not in categories:
            categories.append(v)
    return {
        "id": item.get("exhibitorID", ""),
        "name": item.get("name", ""),
        "description": description,
        "booths": [b.get("booth", "") for b in item.get("booths", []) or []],
        "categories": categories,
        "url": item.get("url", ""),
        "raw_tags": {k: v for k, v in attrs.items() if k not in NOISE_ATTRIBUTES},
    }


def _get_json(url: str, params: dict) -> dict:
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{qs}", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ring_centroid(ring: list) -> tuple:
    """Polygon centroid of one ring [[x, y], ...] via the shoelace formula."""
    area = cx = cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area) < 1e-12:  # degenerate ring: fall back to vertex average
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)
    area *= 0.5
    return cx / (6 * area), cy / (6 * area)


def fetch_rooms() -> list:
    """Snapshot the event-map rooms that sessions are scheduled in.

    Pulls Units (layer 3) rows that carry an RF_RoomName — the exact
    "Room | Building" string RainFocus uses — plus the Levels (layer 4)
    lookup for floor names, and reduces each room polygon to a WGS84
    centroid. Paged politely like the catalog fetches.
    """
    print("Fetching event-map levels (floors)...")
    levels_resp = _get_json(
        f"{EVENT_MAP_BASE}/4/query",
        {"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json"},
    )
    levels = {
        f["attributes"]["LEVEL_ID"]: f["attributes"]
        for f in levels_resp.get("features", [])
    }
    (RAW_DIR / "event_map_levels.json").write_text(
        json.dumps(levels_resp, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    print("Fetching event-map rooms (RF-linked units)...")
    rooms: list = []
    offset = 0
    while True:
        time.sleep(DELAY_SECONDS)
        data = _get_json(
            f"{EVENT_MAP_BASE}/3/query",
            {
                # RF-linked units are the rooms RainFocus schedules into; the
                # expo-floor theaters host sessions too but carry no RF link,
                # so pull them by USE_TYPE. Unit NAME matches the session room
                # string ("Room 33 B", "Demo Expo Theater 2") even when the
                # RF link points at a combined room ("Room 33 ABC | SDCC").
                "where": (
                    "RF_RoomName IS NOT NULL OR "
                    "USE_TYPE IN ('Demo Theater','Spotlight Theater')"
                ),
                "outFields": "NAME,NAME_LONG,LEVEL_ID,USE_TYPE,RF_RoomName,LocationID",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": offset,
                "resultRecordCount": 100,
                "f": "json",
            },
        )
        if data.get("error"):
            raise RuntimeError(f"event map query failed: {data['error']}")
        feats = data.get("features", [])
        (RAW_DIR / f"event_map_units_{offset:05d}.json").write_text(
            json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        for f in feats:
            a = f.get("attributes", {})
            rings = (f.get("geometry") or {}).get("rings") or []
            lng, lat = _ring_centroid(rings[0]) if rings else (None, None)
            level = levels.get(a.get("LEVEL_ID") or "", {})
            name = (a.get("NAME") or "").strip()
            # Building prefix from NAME_LONG ("Hilton - Aqua 300 A" -> Hilton),
            # falling back to the RF room key suffix.
            name_long = a.get("NAME_LONG") or ""
            building = name_long.split(" - ")[0].strip() if " - " in name_long else ""
            rf_key = (a.get("RF_RoomName") or "").strip()
            if not building and "|" in rf_key:
                building = rf_key.rpartition("|")[2].strip()
            location_id = a.get("LocationID") or ""
            rooms.append(
                {
                    "room_key": rf_key
                    or (f"{name} | {building}" if building else name),
                    "room": name,
                    "venue_building": building,
                    "name_long": a.get("NAME_LONG", ""),
                    "use_type": a.get("USE_TYPE", ""),
                    "level_name": level.get("NAME", ""),
                    "level_number": level.get("LEVEL_NUMBER"),
                    "location_id": location_id,
                    "lat": round(lat, 6) if lat is not None else None,
                    "lng": round(lng, 6) if lng is not None else None,
                    "map_url": (
                        f"{EVENT_MAP_APP}?roomid={location_id}" if location_id else ""
                    ),
                }
            )
        print(f"  rooms: {len(rooms)} (page at offset {offset})")
        if not data.get("exceededTransferLimit") or not feats:
            break
        offset += len(feats)
    rooms.sort(key=lambda r: r["room_key"].lower())
    return rooms


def main() -> int:
    taken_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base_meta = {
        "snapshot_taken_at": taken_at,
        "source": "RainFocus public catalog (event.esri.com, widget esri/26uc)",
        "rf_widget_id": RF_WIDGET_ID,
    }

    print("Fetching sessions (all UC-week event tabs)...")
    session_form = {
        "search": "",
        "type": "session",
        "browserTimezone": "America/Los_Angeles",
        "catalogDisplay": "list",
    }
    raw_sessions = fetch_all(SESSIONS_URL, session_form, "sessions")
    sessions = [normalize_session(s) for s in raw_sessions]
    sessions = [s for s in sessions if s["id"] and s["title"]]
    sessions.sort(
        key=lambda s: (
            s["occurrences"][0]["start_iso"] if s["occurrences"] else "9999",
            s["title"],
        )
    )

    time.sleep(DELAY_SECONDS)
    print("Fetching exhibitors...")
    exhibitor_form = {
        "search": "",
        "type": "exhibitor",
        "browserTimezone": "America/Los_Angeles",
        "catalogDisplay": "list",
    }
    raw_exhibitors = fetch_all(SEARCH_URL, exhibitor_form, "exhibitors")
    exhibitors = [normalize_exhibitor(e) for e in raw_exhibitors]
    exhibitors = [e for e in exhibitors if e["id"] and e["name"]]
    exhibitors.sort(key=lambda e: e["name"].lower())

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sessions_doc = {
        "snapshot": {**base_meta, "record_count": len(sessions)},
        "sessions": sessions,
    }
    exhibitors_doc = {
        "snapshot": {**base_meta, "record_count": len(exhibitors)},
        "exhibitors": exhibitors,
    }
    (DATA_DIR / "sessions.json").write_text(
        json.dumps(sessions_doc, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    (DATA_DIR / "exhibitors.json").write_text(
        json.dumps(exhibitors_doc, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    time.sleep(DELAY_SECONDS)
    rooms = fetch_rooms()
    rooms_doc = {
        "snapshot": {
            "snapshot_taken_at": taken_at,
            "source": f"Esri UC Event Map ({EVENT_MAP_BASE})",
            "record_count": len(rooms),
        },
        "rooms": rooms,
    }
    (DATA_DIR / "rooms.json").write_text(
        json.dumps(rooms_doc, indent=1, ensure_ascii=False), encoding="utf-8"
    )

    print(
        f"Wrote {len(sessions)} sessions, {len(exhibitors)} exhibitors, "
        f"and {len(rooms)} rooms (snapshot_taken_at={taken_at})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
