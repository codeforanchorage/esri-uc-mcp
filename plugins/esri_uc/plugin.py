"""Esri UC 2026 conference catalog plugin for OpenContext.

Serves the Esri User Conference session catalog, expo directory, and schedule
from a STATIC SNAPSHOT bundled with the deployment (data/*.json, produced by
scripts/snapshot.py from the RainFocus public catalog). Unlike every other
plugin in the fleet, this one makes ZERO upstream calls at runtime — a catalog
refresh is a re-snapshot and redeploy.

All times are venue-local (America/Los_Angeles); the venue timezone is a
constant of the conference, not a parameter.
"""

import json
import logging
import re
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
from plugins.esri_uc.config_schema import EsriUCPluginConfig

logger = logging.getLogger(__name__)

VENUE_TZ = ZoneInfo("America/Los_Angeles")

DISCLAIMER = (
    "Room changes happen — attendees should verify last-minute changes in the "
    "Esri Events app. Reservations/waitlists are not visible to this server."
)

MAX_RESULTS = 25
MAX_PLAN_CANDIDATES = 20


class EsriUCPlugin(MCPPlugin):
    """Read-only tools over the bundled Esri UC catalog snapshot."""

    plugin_name = "esri_uc"
    plugin_type = PluginType.CUSTOM_API
    plugin_version = "1.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[EsriUCPluginConfig] = None
        self.sessions: List[Dict[str, Any]] = []
        self.exhibitors: List[Dict[str, Any]] = []
        self.rooms: List[Dict[str, Any]] = []
        self.snapshot_meta: Dict[str, Any] = {}
        self._by_id: Dict[str, Dict[str, Any]] = {}
        self._by_code: Dict[str, Dict[str, Any]] = {}
        self._day_to_date: Dict[str, str] = {}

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        try:
            self.plugin_config = EsriUCPluginConfig(**self.config)
            base = Path(__file__).resolve().parent.parent.parent

            sessions_path = self._resolve(base, self.plugin_config.sessions_file)
            doc = json.loads(sessions_path.read_text(encoding="utf-8"))
            self.sessions = doc.get("sessions", [])
            self.snapshot_meta = doc.get("snapshot", {})

            exhibitors_path = self._resolve(base, self.plugin_config.exhibitors_file)
            if exhibitors_path.exists():
                edoc = json.loads(exhibitors_path.read_text(encoding="utf-8"))
                self.exhibitors = edoc.get("exhibitors", [])

            rooms_path = self._resolve(base, self.plugin_config.rooms_file)
            if rooms_path.exists():
                rdoc = json.loads(rooms_path.read_text(encoding="utf-8"))
                self.rooms = rdoc.get("rooms", [])

            if not self.sessions:
                logger.error("esri_uc: sessions snapshot is empty (%s)", sessions_path)
                return False

            for s in self.sessions:
                if s.get("id"):
                    self._by_id[s["id"]] = s
                if s.get("code"):
                    self._by_code[str(s["code"]).lower()] = s
                for occ in s.get("occurrences", []):
                    day, date = occ.get("day", ""), occ.get("date", "")
                    if day and date:
                        self._day_to_date.setdefault(day.lower(), date)

            self._initialized = True
            logger.info(
                "esri_uc plugin initialized: %d sessions, %d exhibitors (snapshot %s)",
                len(self.sessions),
                len(self.exhibitors),
                self.snapshot_meta.get("snapshot_taken_at", "?"),
            )
            return True
        except Exception as e:
            logger.error("Failed to initialize esri_uc plugin: %s", e, exc_info=True)
            return False

    @staticmethod
    def _resolve(base: Path, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else base / path

    async def shutdown(self) -> None:
        self._initialized = False
        logger.info("esri_uc plugin shut down")

    async def health_check(self) -> bool:
        # No upstream to reach — healthy means the snapshot is loaded.
        return self._initialized and bool(self.sessions)

    # ── Tool definitions ────────────────────────────────────────────────

    @property
    def _snapshot_date(self) -> str:
        iso = self.snapshot_meta.get("snapshot_taken_at", "")
        return iso[:10] if iso else "unknown date"

    def get_tools(self) -> List[ToolDefinition]:
        # Closed dataset: openWorldHint False — the snapshot is the whole world.
        read_only = {"readOnlyHint": True, "openWorldHint": False}
        prov = (
            f"Data is a static snapshot of the RainFocus public catalog taken "
            f"{self._snapshot_date}; it never queries live systems. {DISCLAIMER}"
        )
        tools = [
            ToolDefinition(
                name="search_sessions",
                description=(
                    "Keyword search over the Esri UC 2026 session catalog "
                    "(title, abstract, speakers, session code), optionally "
                    "filtered by day, session type, track/topic, or start-time "
                    "window. Returns compact rows (code, title, day/time, room, "
                    "type) capped at 25 with the total match count — use "
                    "get_session for the full record. Filter spellings are "
                    f"discoverable via get_tracks_and_types. {prov}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Keywords (all must match). Matches title, "
                                "abstract, speaker names/orgs, and code. Empty "
                                "string lists everything matching the filters."
                            ),
                        },
                        "day": {
                            "type": "string",
                            "description": (
                                "Conference day: weekday name ('Wednesday'), "
                                "3-letter prefix ('wed'), or date '2026-07-15'. "
                                "UC 2026 runs Mon Jul 13 – Fri Jul 17 (a few "
                                "Sunday activities exist)."
                            ),
                        },
                        "type": {
                            "type": "string",
                            "description": (
                                "Session type, e.g. 'Technical Session', 'Demo "
                                "Theater Presentation', 'Spotlight Session', "
                                "'Special Interest Group Meeting', 'User "
                                "Presentations'."
                            ),
                        },
                        "track": {
                            "type": "string",
                            "description": "Track/topic tag, e.g. 'ArcGIS Pro', 'Climate Action' (substring ok)",
                        },
                        "after": {
                            "type": "string",
                            "description": "Only sessions starting at/after this venue-local time, 24h 'HH:MM'",
                        },
                        "before": {
                            "type": "string",
                            "description": "Only sessions starting before this venue-local time, 24h 'HH:MM'",
                        },
                    },
                    "required": ["query"],
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_session",
                description=(
                    "Full record for one session by id or session code (e.g. "
                    "'AGE1998' or a RainFocus sessionID): abstract, speakers "
                    "with organizations, day/time/room, venue building, tracks, "
                    f"livestream flag, and raw catalog tags. {prov}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "id_or_code": {
                            "type": "string",
                            "description": "Session code (case-insensitive) or sessionID from search results",
                        },
                    },
                    "required": ["id_or_code"],
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="whats_on",
                description=(
                    "Time-aware view of the Esri UC 2026 schedule: mode='now' "
                    "lists sessions in progress at the reference time; "
                    "mode='next' lists sessions starting within the next "
                    "within_minutes (default 90). Reference time defaults to "
                    "the current clock converted to venue time "
                    "(America/Los_Angeles); pass 'at' (ISO, e.g. "
                    "'2026-07-15T14:00') to ask about any other moment. "
                    f"Optional topic filter. {prov}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["now", "next"],
                            "description": "'now' = in progress; 'next' = starting soon",
                            "default": "now",
                        },
                        "topic": {
                            "type": "string",
                            "description": "Optional keyword filter (matches title, abstract, tracks)",
                        },
                        "within_minutes": {
                            "type": "integer",
                            "description": "For mode='next': look-ahead window in minutes",
                            "default": 90,
                            "minimum": 1,
                            "maximum": 720,
                        },
                        "at": {
                            "type": "string",
                            "description": (
                                "Optional reference time override, ISO 8601. "
                                "Naive values are taken as venue-local "
                                "(America/Los_Angeles)."
                            ),
                        },
                    },
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="plan_block",
                description=(
                    "Plan a block of conference time: given a day, a venue-local "
                    "start/end window, and topics of interest, returns candidate "
                    "sessions in the window sorted by topic relevance, with "
                    "overlap conflicts between candidates explicitly marked. "
                    "Presents options only — it does not pick a schedule. "
                    f"{prov}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "day": {
                            "type": "string",
                            "description": "Weekday name, 3-letter prefix, or date '2026-07-15'",
                        },
                        "start": {
                            "type": "string",
                            "description": "Window start, venue-local 24h 'HH:MM'",
                        },
                        "end": {
                            "type": "string",
                            "description": "Window end, venue-local 24h 'HH:MM'",
                        },
                        "topics": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Topics/keywords to rank candidates by (matched against title, abstract, tracks)",
                        },
                    },
                    "required": ["day", "start", "end", "topics"],
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="find_exhibitors",
                description=(
                    "Search the Esri UC 2026 expo directory (321 exhibitors) by "
                    "keyword and/or category, returning booth numbers, a short "
                    "description, and website. Categories include Exhibitor, "
                    "Showcase, Sponsor tiers, Startup Zone, Academic Fair, Map "
                    f"Lounge. {prov}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keywords over exhibitor name and description",
                        },
                        "category": {
                            "type": "string",
                            "description": "Category filter (substring, e.g. 'Startup', 'Sponsor')",
                        },
                    },
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_tracks_and_types",
                description=(
                    "Discover the exact vocabulary of the snapshot: distinct "
                    "session types, tracks/topics, days, venue buildings, "
                    "session levels, and exhibitor categories, each with counts. "
                    "Call this before guessing filter values for search_sessions "
                    f"or find_exhibitors. {prov}"
                ),
                input_schema={"type": "object", "properties": {}},
                annotations=read_only,
            ),
        ]
        if self.rooms:
            tools.append(
                ToolDefinition(
                    name="where_is_room",
                    description=(
                        "Locate a conference room: building (SDCC / Marriott "
                        "Marquis / Hilton Bayfront), floor, WGS84 lat/lng of "
                        "the room centroid, and a deep link into the official "
                        "UC event map. Room geometry comes from the event "
                        "map's own ArcGIS service — the venue is itself a "
                        "feature service. Pass the room as it appears in "
                        "session results (e.g. 'Room 33 B'). Coordinates can "
                        "be composed with maps/places tools for walking-"
                        f"distance questions. {prov}"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "room_name": {
                                "type": "string",
                                "description": (
                                    "Room name from a session result, with or "
                                    "without the building suffix ('Ballroom 06' "
                                    "or 'Ballroom 06 | SDCC')"
                                ),
                            },
                        },
                        "required": ["room_name"],
                    },
                    annotations=read_only,
                )
            )
        return tools

    # ── Tool dispatch ───────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "search_sessions":
                return self._ok(self._search_sessions(arguments))
            if tool_name == "get_session":
                key = (arguments.get("id_or_code") or "").strip()
                if not key:
                    return self._err("id_or_code is required")
                return self._ok(self._get_session(key))
            if tool_name == "whats_on":
                return self._ok(self._whats_on(arguments))
            if tool_name == "plan_block":
                missing = [
                    k
                    for k in ("day", "start", "end", "topics")
                    if arguments.get(k) in (None, "", [])
                ]
                if missing:
                    return self._err(
                        f"Missing required argument(s): {', '.join(missing)}"
                    )
                return self._ok(self._plan_block(arguments))
            if tool_name == "find_exhibitors":
                return self._ok(self._find_exhibitors(arguments))
            if tool_name == "get_tracks_and_types":
                return self._ok(self._tracks_and_types())
            if tool_name == "where_is_room" and self.rooms:
                room_name = (arguments.get("room_name") or "").strip()
                if not room_name:
                    return self._err("room_name is required")
                return self._ok(self._where_is_room(room_name))
            return self._err(f"Unknown tool: {tool_name}")
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:
            logger.error("Error executing tool %s: %s", tool_name, e, exc_info=True)
            return self._err(str(e) or "Tool execution failed")

    # ── Result helpers ──────────────────────────────────────────────────

    def _footer(self) -> str:
        return f"\n—\nSchedule snapshot taken {self._snapshot_date}. {DISCLAIMER}"

    def _no_match(self, what: str) -> str:
        return (
            f"No {what} match in the snapshot (taken {self._snapshot_date}). "
            "That is not proof none exists — the live catalog may have changed "
            "since the snapshot; check the Esri Events app to be sure." + self._footer()
        )

    @staticmethod
    def _ok(text: str) -> ToolResult:
        return ToolResult(content=[{"type": "text", "text": text}], success=True)

    @staticmethod
    def _err(message: str) -> ToolResult:
        return ToolResult(content=[], success=False, error_message=message)

    # ── Time & matching helpers ─────────────────────────────────────────

    @staticmethod
    def _parse_hhmm(value: str, label: str) -> dtime:
        m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
        if not m:
            raise ValueError(f"{label} must be venue-local 24h 'HH:MM' (got {value!r})")
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ValueError(f"{label} out of range: {value!r}")
        return dtime(h, mi)

    def _resolve_day(self, value: str) -> str:
        """Resolve a day argument to an ISO date present in the snapshot."""
        v = (value or "").strip().lower()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return v
        for day, date in self._day_to_date.items():
            if day.startswith(v) and len(v) >= 3:
                return date
        known = ", ".join(
            f"{d.capitalize()} ({dt})"
            for d, dt in sorted(self._day_to_date.items(), key=lambda kv: kv[1])
        )
        raise ValueError(f"Unknown day {value!r}. Days in the snapshot: {known}")

    def _reference_time(self, at: Optional[str]) -> datetime:
        if at:
            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                raise ValueError(f"'at' is not valid ISO 8601: {at!r}")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=VENUE_TZ)
            return dt.astimezone(VENUE_TZ)
        return datetime.now(timezone.utc).astimezone(VENUE_TZ)

    @staticmethod
    def _occ_times(occ: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
        try:
            start = datetime.fromisoformat(occ["start_iso"])
            end = datetime.fromisoformat(occ["end_iso"])
            return start, end
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _haystack(s: Dict[str, Any]) -> str:
        parts = [
            s.get("title", ""),
            s.get("abstract", ""),
            str(s.get("code", "")),
            " ".join(s.get("tracks", [])),
            " ".join(
                f"{sp.get('name', '')} {sp.get('org', '')}"
                for sp in s.get("speakers", [])
            ),
        ]
        return " ".join(parts).lower()

    @staticmethod
    def _matches_all(haystack: str, query: str) -> bool:
        """Every token must appear; short tokens ('AI', 'MCP', '3D') must match
        whole words so 'AI' doesn't hit 'available' or 'maintain'."""
        for tok in query.lower().split():
            if len(tok) <= 3:
                if not re.search(rf"\b{re.escape(tok)}\b", haystack):
                    return False
            elif tok not in haystack:
                return False
        return True

    # ── Row formatting ──────────────────────────────────────────────────

    @staticmethod
    def _fmt_time(dt: datetime) -> str:
        return dt.strftime("%I:%M %p").lstrip("0")

    def _fmt_occ(self, occ: Dict[str, Any]) -> str:
        times = self._occ_times(occ)
        when = "time TBD"
        if times:
            start, end = times
            when = (
                f"{occ.get('day', '')[:3]} {start.strftime('%b %d')} "
                f"{self._fmt_time(start)}–{self._fmt_time(end)}"
            )
        room = occ.get("room", "") or "room TBD"
        building = occ.get("venue_building", "")
        loc = f"{room} | {building}" if building else room
        return f"{when} · {loc}"

    def _session_row(
        self, s: Dict[str, Any], occ: Optional[Dict[str, Any]] = None
    ) -> str:
        occs = [occ] if occ else (s.get("occurrences") or [None])
        first = occs[0]
        when_where = self._fmt_occ(first) if first else "unscheduled"
        live = " · livestream" if s.get("livestream") else ""
        return (
            f"[{s.get('code', '?')}] {s.get('title', 'Untitled')}\n"
            f"    {when_where} · {s.get('type', '')}{live}"
        )

    # ── search_sessions ─────────────────────────────────────────────────

    def _search_sessions(self, args: Dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        day = args.get("day")
        stype = (args.get("type") or "").strip().lower()
        track = (args.get("track") or "").strip().lower()
        after = self._parse_hhmm(args["after"], "after") if args.get("after") else None
        before = (
            self._parse_hhmm(args["before"], "before") if args.get("before") else None
        )
        date = self._resolve_day(day) if day else None

        matches: List[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]] = []
        for s in self.sessions:
            if query and not self._matches_all(self._haystack(s), query):
                continue
            if stype and s.get("type", "").lower() != stype:
                continue
            if track and not any(track in t.lower() for t in s.get("tracks", [])):
                continue
            occs = s.get("occurrences", [])
            if date or after or before:
                fitting = []
                for occ in occs:
                    if date and occ.get("date") != date:
                        continue
                    times = self._occ_times(occ)
                    if not times:
                        continue
                    local_start = times[0].timetz().replace(tzinfo=None)
                    if after and local_start < after:
                        continue
                    if before and local_start >= before:
                        continue
                    fitting.append(occ)
                if not fitting:
                    continue
                matches.append((s, fitting[0]))
            else:
                matches.append((s, occs[0] if occs else None))

        if not matches:
            return self._no_match("sessions")

        def sort_key(pair):
            s, occ = pair
            return (occ or {}).get("start_iso") or "9999"

        matches.sort(key=sort_key)
        shown = matches[:MAX_RESULTS]
        lines = [
            f"{len(matches)} session(s) match"
            + (f" (showing first {len(shown)})" if len(matches) > len(shown) else "")
            + ":",
            "",
        ]
        for s, occ in shown:
            lines.append(self._session_row(s, occ))
        lines.append("")
        lines.append("Use get_session(<code>) for abstract and speakers.")
        return "\n".join(lines) + self._footer()

    # ── get_session ─────────────────────────────────────────────────────

    def _get_session(self, key: str) -> str:
        s = self._by_id.get(key) or self._by_code.get(key.lower())
        if not s:
            return self._no_match(f"sessions with id/code {key!r}")

        lines = [
            f"{s.get('title', 'Untitled')}",
            f"code: {s.get('code', '')}   id: {s.get('id', '')}",
            f"type: {s.get('type', '')}"
            + (f"   level: {s['level']}" if s.get("level") else "")
            + (f"   event: {s['event']}" if s.get("event") else ""),
        ]
        for occ in s.get("occurrences", []):
            lines.append(f"when/where: {self._fmt_occ(occ)}")
            if occ.get("room_map_url"):
                lines.append(f"room map: {occ['room_map_url']}")
        if s.get("livestream"):
            lines.append("livestream: yes")
        if s.get("tracks"):
            lines.append(f"tracks: {', '.join(s['tracks'])}")
        raw = s.get("raw_tags", {})
        for label, attr in (
            ("products", "EsriProducts"),
            ("industries", "Industry"),
        ):
            if raw.get(attr):
                lines.append(f"{label}: {', '.join(raw[attr])}")
        if s.get("speakers"):
            lines.append("speakers:")
            for sp in s["speakers"]:
                org = f" ({sp['org']})" if sp.get("org") else ""
                lines.append(f"  - {sp['name']}{org}")
        if s.get("abstract"):
            lines.append("")
            lines.append(s["abstract"])
        return "\n".join(lines) + self._footer()

    # ── whats_on ────────────────────────────────────────────────────────

    def _whats_on(self, args: Dict[str, Any]) -> str:
        mode = (args.get("mode") or "now").lower()
        if mode not in ("now", "next"):
            raise ValueError(f"mode must be 'now' or 'next' (got {mode!r})")
        topic = (args.get("topic") or "").strip()
        within = int(args.get("within_minutes") or 90)
        ref = self._reference_time(args.get("at"))
        horizon = ref + timedelta(minutes=within)

        rows: List[Tuple[datetime, Dict[str, Any], Dict[str, Any]]] = []
        for s in self.sessions:
            if topic and not self._matches_all(self._haystack(s), topic):
                continue
            for occ in s.get("occurrences", []):
                times = self._occ_times(occ)
                if not times:
                    continue
                start, end = times
                if mode == "now" and start <= ref <= end:
                    rows.append((start, s, occ))
                elif mode == "next" and ref < start <= horizon:
                    rows.append((start, s, occ))

        ref_str = ref.strftime("%A %b %d, %I:%M %p").replace(" 0", " ")
        what = (
            f"sessions in progress at {ref_str} (venue time)"
            if mode == "now"
            else f"sessions starting within {within} min of {ref_str} (venue time)"
        )
        if not rows:
            return self._no_match(what)

        rows.sort(key=lambda r: (r[0], r[1].get("title", "")))
        shown = rows[:MAX_RESULTS]
        lines = [
            f"{len(rows)} {what}"
            + (f" (showing first {len(shown)})" if len(rows) > len(shown) else "")
            + ":",
            "",
        ]
        for _, s, occ in shown:
            lines.append(self._session_row(s, occ))
        return "\n".join(lines) + self._footer()

    # ── plan_block ──────────────────────────────────────────────────────

    def _plan_block(self, args: Dict[str, Any]) -> str:
        date = self._resolve_day(args["day"])
        start_t = self._parse_hhmm(args["start"], "start")
        end_t = self._parse_hhmm(args["end"], "end")
        if end_t <= start_t:
            raise ValueError("end must be after start")
        topics = [t.strip() for t in args.get("topics", []) if t and t.strip()]

        win_start = datetime.combine(
            datetime.strptime(date, "%Y-%m-%d").date(), start_t, tzinfo=VENUE_TZ
        )
        win_end = datetime.combine(win_start.date(), end_t, tzinfo=VENUE_TZ)

        candidates = []
        for s in self.sessions:
            hay = self._haystack(s)
            score = sum(1 for t in topics if self._matches_all(hay, t))
            if topics and score == 0:
                continue
            for occ in s.get("occurrences", []):
                if occ.get("date") != date:
                    continue
                times = self._occ_times(occ)
                if not times:
                    continue
                st, en = times
                if st < win_end and en > win_start:  # overlaps window
                    candidates.append(
                        {"s": s, "occ": occ, "start": st, "end": en, "score": score}
                    )

        window_str = (
            f"{win_start.strftime('%A %b %d')} "
            f"{self._fmt_time(win_start)}–{self._fmt_time(win_end)}"
        )
        if not candidates:
            return self._no_match(
                f"sessions in the {window_str} window matching {topics}"
            )

        candidates.sort(key=lambda c: (-c["score"], c["start"]))
        truncated = len(candidates) > MAX_PLAN_CANDIDATES
        candidates = candidates[:MAX_PLAN_CANDIDATES]

        # Pairwise overlap conflicts among the presented candidates.
        conflicts: Dict[str, List[str]] = {}
        for i, a in enumerate(candidates):
            for b in candidates[i + 1 :]:
                if a["start"] < b["end"] and b["start"] < a["end"]:
                    a_code = str(a["s"].get("code", "?"))
                    b_code = str(b["s"].get("code", "?"))
                    conflicts.setdefault(a_code, []).append(b_code)
                    conflicts.setdefault(b_code, []).append(a_code)

        lines = [
            f"{len(candidates)} candidate session(s) in {window_str}, "
            f"ranked by match on {topics}:"
            + (" (list truncated — narrow the topics or window)" if truncated else ""),
            "",
        ]
        for c in candidates:
            code = str(c["s"].get("code", "?"))
            lines.append(self._session_row(c["s"], c["occ"]))
            lines.append(f"    topic matches: {c['score']}/{len(topics)}")
            if code in conflicts:
                lines.append(
                    f"    ⚠ overlaps with: {', '.join(sorted(set(conflicts[code])))}"
                )
        lines.append("")
        lines.append(
            "These are options, not a schedule — sessions marked ⚠ cannot all "
            "be attended; choose per priority."
        )
        return "\n".join(lines) + self._footer()

    # ── find_exhibitors ─────────────────────────────────────────────────

    def _find_exhibitors(self, args: Dict[str, Any]) -> str:
        query = (args.get("query") or "").strip()
        category = (args.get("category") or "").strip().lower()

        matches = []
        for e in self.exhibitors:
            hay = f"{e.get('name', '')} {e.get('description', '')}".lower()
            if query and not self._matches_all(hay, query):
                continue
            if category and not any(
                category in c.lower() for c in e.get("categories", [])
            ):
                continue
            matches.append(e)

        if not matches:
            return self._no_match("exhibitors")

        shown = matches[:MAX_RESULTS]
        lines = [
            f"{len(matches)} exhibitor(s) match"
            + (f" (showing first {len(shown)})" if len(matches) > len(shown) else "")
            + ":",
            "",
        ]
        for e in shown:
            booths = ", ".join(b for b in e.get("booths", []) if b) or "no booth listed"
            cats = ", ".join(e.get("categories", []))
            desc = e.get("description", "")
            if len(desc) > 200:
                desc = desc[:200] + "…"
            lines.append(f"{e.get('name', '?')} — booth {booths}")
            if cats:
                lines.append(f"    {cats}")
            if desc:
                lines.append(f"    {desc}")
            if e.get("url"):
                lines.append(f"    {e['url']}")
        return "\n".join(lines) + self._footer()

    # ── where_is_room ───────────────────────────────────────────────────

    @staticmethod
    def _room_tokens(name: str) -> List[str]:
        # Lowercase tokens with zero-padded numbers normalized ('06' == '6').
        return [
            t.lstrip("0") or "0" if t.isdigit() else t for t in name.lower().split()
        ]

    def _where_is_room(self, room_name: str) -> str:
        q = room_name.strip()
        # Optional '| Building' suffix narrows by building.
        q_building = ""
        if "|" in q:
            q, _, q_building = q.rpartition("|")
            q, q_building = q.strip(), q_building.strip().lower()

        def matches(r: Dict[str, Any]) -> bool:
            if q_building and r.get("venue_building", "").lower() != q_building:
                return False
            rtokens = self._room_tokens(r.get("room", ""))
            for t in self._room_tokens(q):
                # Short alpha tokens match inside combined-section tokens,
                # so 'B' finds 'Room 33 B' and also matches section 'BC'.
                if not any(
                    t == rt or (t.isalpha() and len(t) <= 2 and t in rt)
                    for rt in rtokens
                ):
                    return False
            return True

        exact = [r for r in self.rooms if r.get("room", "").lower() == q.lower()]
        hits = exact or [r for r in self.rooms if matches(r)]
        # Prefer the most specific (shortest) names when several sections match.
        hits.sort(key=lambda r: len(r.get("room", "")))

        if not hits:
            return self._no_match(f"rooms named {room_name!r}")

        if len(hits) > 1:
            lines = [
                f"{len(hits)} rooms match {room_name!r} — be more specific:",
                "",
            ]
            for r in hits[:8]:
                lines.append(f"  {r.get('room_key', '')}")
            return "\n".join(lines) + self._footer()

        r = hits[0]
        building = {
            "SDCC": "San Diego Convention Center (SDCC)",
            "Marriott": "Marriott Marquis San Diego Marina",
            "Hilton": "Hilton San Diego Bayfront",
        }.get(r.get("venue_building", ""), r.get("venue_building", ""))
        lines = [
            f"{r.get('room', room_name)}",
            f"building: {building}",
            f"floor: {r.get('level_name', '')}",
        ]
        if r.get("use_type"):
            lines.append(f"space type: {r['use_type']}")
        if r.get("lat") is not None:
            lines.append(f"location: {r['lat']}, {r['lng']} (WGS84 room centroid)")
        if r.get("map_url"):
            lines.append(f"event map: {r['map_url']}")
        return "\n".join(lines) + self._footer()

    # ── get_tracks_and_types ────────────────────────────────────────────

    def _tracks_and_types(self) -> str:
        def counted(values: List[str]) -> List[str]:
            counts: Dict[str, int] = {}
            for v in values:
                counts[v] = counts.get(v, 0) + 1
            return [
                f"  {v} ({n})"
                for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            ]

        types = [s.get("type", "") for s in self.sessions if s.get("type")]
        tracks = [t for s in self.sessions for t in s.get("tracks", [])]
        levels = [s.get("level", "") for s in self.sessions if s.get("level")]
        days = [
            f"{o.get('day', '')} ({o.get('date', '')})"
            for s in self.sessions
            for o in s.get("occurrences", [])
            if o.get("day")
        ]
        buildings = [
            o.get("venue_building", "")
            for s in self.sessions
            for o in s.get("occurrences", [])
            if o.get("venue_building")
        ]
        ex_cats = [c for e in self.exhibitors for c in e.get("categories", [])]

        sections = [
            ("Session types", counted(types)),
            ("Days", counted(days)),
            ("Venue buildings", counted(buildings)),
            ("Session levels", counted(levels)),
            ("Tracks / topics", counted(tracks)),
            ("Exhibitor categories", counted(ex_cats)),
        ]
        lines: List[str] = []
        for title, rows in sections:
            lines.append(f"{title}:")
            lines.extend(rows or ["  (none)"])
            lines.append("")
        return "\n".join(lines).rstrip() + self._footer()
