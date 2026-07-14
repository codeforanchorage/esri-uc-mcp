"""Tests for the Esri UC conference catalog plugin.

The plugin serves a bundled static snapshot, so tests run against small
fixture files written to tmp_path — no network, no mocks of HTTP clients.
Covers initialization, every tool, time handling (venue timezone, 'at'
override), conflict marking, filter vocabulary, and the no-match phrasing
("no matches in the snapshot", never authoritative absence).
"""

import json

import pytest

from core.interfaces import PluginType
from plugins.esri_uc.config_schema import EsriUCPluginConfig
from plugins.esri_uc.plugin import EsriUCPlugin

SNAPSHOT_META = {
    "snapshot_taken_at": "2026-07-14T05:51:13+00:00",
    "source": "RainFocus public catalog (event.esri.com, widget esri/26uc)",
    "rf_widget_id": "test-widget",
}


def _session(
    sid,
    code,
    title,
    day="Wednesday",
    date="2026-07-15",
    start="2026-07-15T13:00:00-07:00",
    end="2026-07-15T14:00:00-07:00",
    **over,
):
    s = {
        "id": sid,
        "code": code,
        "title": title,
        "abstract": over.pop("abstract", f"Abstract for {title}."),
        "type": over.pop("type", "Technical Session"),
        "tracks": over.pop("tracks", ["ArcGIS Pro"]),
        "event": "Esri User Conference",
        "level": "All Attendees",
        "occurrences": over.pop(
            "occurrences",
            [
                {
                    "day": day,
                    "date": date,
                    "start_iso": start,
                    "end_iso": end,
                    "room": over.pop("room", "Room 01 A"),
                    "venue_building": over.pop("venue_building", "SDCC"),
                    "room_map_url": "https://example.com/map?roomid=x",
                }
            ],
        ),
        "speakers": over.pop("speakers", [{"name": "Ada Alvarez", "org": "Esri"}]),
        "livestream": over.pop("livestream", False),
        "raw_tags": over.pop("raw_tags", {"EsriProducts": ["ArcGIS Pro"]}),
    }
    s.update(over)
    return s


FIXTURE_SESSIONS = [
    _session(
        "id-mcp",
        "AI-100",
        "Building MCP Servers for GIS",
        abstract="Model Context Protocol servers with agentic AI workflows.",
        tracks=["Artificial Intelligence"],
        livestream=True,
    ),
    _session(
        "id-overlap",
        "AI-101",
        "Agentic AI in Local Government",
        start="2026-07-15T13:30:00-07:00",
        end="2026-07-15T14:30:00-07:00",
        tracks=["Artificial Intelligence", "State and Local Government"],
        speakers=[{"name": "Jay Theodore", "org": "Esri"}],
    ),
    _session(
        "id-later",
        "WA-200",
        "Water Utility Networks Deep Dive",
        start="2026-07-15T15:00:00-07:00",
        end="2026-07-15T16:00:00-07:00",
        type="Demo Theater Presentation",
        tracks=["Water"],
        venue_building="Marriott",
    ),
    _session(
        "id-thu",
        "LA-300",
        "Living Atlas: What's New",
        day="Thursday",
        date="2026-07-16",
        start="2026-07-16T08:30:00-07:00",
        end="2026-07-16T09:30:00-07:00",
        tracks=["Living Atlas"],
    ),
]

FIXTURE_EXHIBITORS = [
    {
        "id": "ex-1",
        "name": "Drone Dynamics",
        "description": "Drone mapping for local government.",
        "booths": ["715"],
        "categories": ["Exhibitor", "Startup Zone Exhibitor"],
        "url": "https://example.com",
        "raw_tags": {},
    },
    {
        "id": "ex-2",
        "name": "Acme Imagery",
        "description": "Satellite imagery products.",
        "booths": ["1201", "1203"],
        "categories": ["Showcase"],
        "url": "",
        "raw_tags": {},
    },
]


FIXTURE_ROOMS = [
    {
        "room_key": "Room 33 ABC | SDCC",
        "room": "Room 33 B",
        "venue_building": "SDCC",
        "name_long": "SDCC - Room 33 B",
        "use_type": "Conference Room",
        "level_name": "Upper Level",
        "level_number": 3,
        "location_id": "loc-33b",
        "lat": 32.705714,
        "lng": -117.161211,
        "map_url": "https://webapps-cdn.esri.com/CDN/uc-event-maps/web.html?roomid=loc-33b",
    },
    {
        "room_key": "Room 33 A | SDCC",
        "room": "Room 33 A",
        "venue_building": "SDCC",
        "name_long": "SDCC - Room 33 A",
        "use_type": "Conference Room",
        "level_name": "Upper Level",
        "level_number": 3,
        "location_id": "loc-33a",
        "lat": 32.7057,
        "lng": -117.1612,
        "map_url": "",
    },
    {
        "room_key": "Vista | Marriott",
        "room": "Vista",
        "venue_building": "Marriott",
        "name_long": "Marriott - Vista",
        "use_type": "Conference Room",
        "level_name": "Marriott - Floor 1",
        "level_number": 1,
        "location_id": "loc-vista",
        "lat": 32.7047,
        "lng": -117.1665,
        "map_url": "",
    },
]


@pytest.fixture
def data_files(tmp_path):
    sessions = tmp_path / "sessions.json"
    exhibitors = tmp_path / "exhibitors.json"
    rooms = tmp_path / "rooms.json"
    rooms.write_text(
        json.dumps(
            {
                "snapshot": {**SNAPSHOT_META, "record_count": len(FIXTURE_ROOMS)},
                "rooms": FIXTURE_ROOMS,
            }
        ),
        encoding="utf-8",
    )
    sessions.write_text(
        json.dumps(
            {
                "snapshot": {**SNAPSHOT_META, "record_count": len(FIXTURE_SESSIONS)},
                "sessions": FIXTURE_SESSIONS,
            }
        ),
        encoding="utf-8",
    )
    exhibitors.write_text(
        json.dumps(
            {
                "snapshot": {**SNAPSHOT_META, "record_count": len(FIXTURE_EXHIBITORS)},
                "exhibitors": FIXTURE_EXHIBITORS,
            }
        ),
        encoding="utf-8",
    )
    return {
        "sessions_file": str(sessions),
        "exhibitors_file": str(exhibitors),
        "rooms_file": str(rooms),
    }


@pytest.fixture
async def plugin(data_files):
    p = EsriUCPlugin(data_files)
    assert await p.initialize() is True
    return p


async def _text(plugin, tool, args=None):
    result = await plugin.execute_tool(tool, args or {})
    assert result.success, result.error_message
    return result.content[0]["text"]


# ── Attributes, config, lifecycle ──────────────────────────────────────


class TestLifecycle:
    def test_attributes(self):
        p = EsriUCPlugin({})
        assert p.plugin_name == "esri_uc"
        assert p.plugin_type == PluginType.CUSTOM_API

    def test_config_defaults(self):
        cfg = EsriUCPluginConfig()
        assert cfg.sessions_file == "data/sessions.json"
        assert cfg.conference_name == "2026 Esri User Conference"

    @pytest.mark.asyncio
    async def test_initialize_and_health(self, data_files):
        p = EsriUCPlugin(data_files)
        assert await p.initialize() is True
        assert p.is_initialized
        assert await p.health_check() is True
        await p.shutdown()
        assert not p.is_initialized
        assert await p.health_check() is False

    @pytest.mark.asyncio
    async def test_initialize_fails_on_missing_file(self, tmp_path):
        p = EsriUCPlugin({"sessions_file": str(tmp_path / "nope.json")})
        assert await p.initialize() is False

    @pytest.mark.asyncio
    async def test_initialize_fails_on_empty_sessions(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text(json.dumps({"snapshot": SNAPSHOT_META, "sessions": []}))
        p = EsriUCPlugin({"sessions_file": str(f)})
        assert await p.initialize() is False

    @pytest.mark.asyncio
    async def test_missing_exhibitors_file_is_tolerated(self, tmp_path, data_files):
        cfg = dict(data_files)
        cfg["exhibitors_file"] = str(tmp_path / "absent.json")
        p = EsriUCPlugin(cfg)
        assert await p.initialize() is True
        assert p.exhibitors == []


# ── Tool definitions ───────────────────────────────────────────────────


class TestToolDefinitions:
    @pytest.mark.asyncio
    async def test_six_tools_with_provenance(self, plugin):
        tools = plugin.get_tools()
        names = {t.name for t in tools}
        assert names == {
            "search_sessions",
            "get_session",
            "whats_on",
            "plan_block",
            "find_exhibitors",
            "get_tracks_and_types",
            "where_is_room",
        }
        for t in tools:
            assert "2026-07-14" in t.description  # snapshot date
            assert "Esri Events app" in t.description
            assert t.annotations == {"readOnlyHint": True, "openWorldHint": False}

    @pytest.mark.asyncio
    async def test_unknown_tool(self, plugin):
        result = await plugin.execute_tool("nope", {})
        assert not result.success
        assert "Unknown tool" in result.error_message


# ── search_sessions ────────────────────────────────────────────────────


class TestSearchSessions:
    @pytest.mark.asyncio
    async def test_keyword_search(self, plugin):
        text = await _text(plugin, "search_sessions", {"query": "MCP"})
        assert "Building MCP Servers for GIS" in text
        assert "AI-100" in text
        assert "1 session(s) match" in text

    @pytest.mark.asyncio
    async def test_short_token_word_boundary(self, plugin):
        # 'ai' must not match 'maintain'/'available' style substrings;
        # only sessions with the word AI (in title/abstract/track) hit.
        text = await _text(plugin, "search_sessions", {"query": "AI"})
        assert "Agentic AI in Local Government" in text
        assert "Water Utility Networks" not in text

    @pytest.mark.asyncio
    async def test_speaker_search(self, plugin):
        text = await _text(plugin, "search_sessions", {"query": "Jay Theodore"})
        assert "Agentic AI in Local Government" in text
        assert "1 session(s)" in text

    @pytest.mark.asyncio
    async def test_day_and_time_filters(self, plugin):
        text = await _text(
            plugin,
            "search_sessions",
            {"query": "", "day": "wed", "after": "14:30", "before": "16:00"},
        )
        assert "Water Utility Networks" in text
        assert "Building MCP Servers" not in text

    @pytest.mark.asyncio
    async def test_type_and_track_filters(self, plugin):
        text = await _text(
            plugin,
            "search_sessions",
            {"query": "", "type": "demo theater presentation"},
        )
        assert "Water Utility Networks" in text
        text = await _text(
            plugin, "search_sessions", {"query": "", "track": "living atlas"}
        )
        assert "Living Atlas: What's New" in text

    @pytest.mark.asyncio
    async def test_no_match_is_not_authoritative(self, plugin):
        text = await _text(plugin, "search_sessions", {"query": "underwater basket"})
        assert "No sessions match in the snapshot" in text
        assert "not proof none exists" in text

    @pytest.mark.asyncio
    async def test_bad_day_lists_known_days(self, plugin):
        result = await plugin.execute_tool(
            "search_sessions", {"query": "", "day": "Saturday"}
        )
        assert not result.success
        assert "Days in the snapshot" in result.error_message

    @pytest.mark.asyncio
    async def test_bad_time_format(self, plugin):
        result = await plugin.execute_tool(
            "search_sessions", {"query": "", "after": "2pm"}
        )
        assert not result.success
        assert "HH:MM" in result.error_message


# ── get_session ────────────────────────────────────────────────────────


class TestGetSession:
    @pytest.mark.asyncio
    async def test_by_code_case_insensitive(self, plugin):
        text = await _text(plugin, "get_session", {"id_or_code": "ai-100"})
        assert "Building MCP Servers for GIS" in text
        assert "Model Context Protocol servers" in text
        assert "Ada Alvarez (Esri)" in text
        assert "livestream: yes" in text
        assert "room map:" in text

    @pytest.mark.asyncio
    async def test_by_id(self, plugin):
        text = await _text(plugin, "get_session", {"id_or_code": "id-thu"})
        assert "Living Atlas" in text

    @pytest.mark.asyncio
    async def test_missing_arg(self, plugin):
        result = await plugin.execute_tool("get_session", {})
        assert not result.success

    @pytest.mark.asyncio
    async def test_unknown_code(self, plugin):
        text = await _text(plugin, "get_session", {"id_or_code": "ZZ-999"})
        assert "No sessions" in text and "snapshot" in text


# ── whats_on ───────────────────────────────────────────────────────────


class TestWhatsOn:
    @pytest.mark.asyncio
    async def test_now_with_at_override(self, plugin):
        text = await _text(
            plugin, "whats_on", {"mode": "now", "at": "2026-07-15T13:15"}
        )
        assert "Building MCP Servers" in text
        assert "Water Utility" not in text

    @pytest.mark.asyncio
    async def test_next_window(self, plugin):
        text = await _text(
            plugin,
            "whats_on",
            {"mode": "next", "at": "2026-07-15T14:00", "within_minutes": 90},
        )
        assert "Water Utility Networks" in text  # starts 15:00
        assert "Building MCP Servers" not in text  # already started

    @pytest.mark.asyncio
    async def test_topic_filter(self, plugin):
        text = await _text(
            plugin,
            "whats_on",
            {
                "mode": "next",
                "at": "2026-07-15T12:00",
                "topic": "local government",
                "within_minutes": 120,
            },
        )
        assert "Agentic AI in Local Government" in text
        assert "Building MCP Servers" not in text

    @pytest.mark.asyncio
    async def test_aware_at_is_converted_to_venue_time(self, plugin):
        # 20:15 UTC == 13:15 PDT, during the MCP session
        text = await _text(
            plugin, "whats_on", {"mode": "now", "at": "2026-07-15T20:15:00+00:00"}
        )
        assert "Building MCP Servers" in text

    @pytest.mark.asyncio
    async def test_empty_when_nothing_on(self, plugin):
        text = await _text(
            plugin, "whats_on", {"mode": "now", "at": "2026-07-15T05:00"}
        )
        assert "No sessions in progress" in text

    @pytest.mark.asyncio
    async def test_invalid_inputs(self, plugin):
        for args in ({"mode": "later"}, {"at": "not-a-date"}):
            result = await plugin.execute_tool("whats_on", args)
            assert not result.success


# ── plan_block ─────────────────────────────────────────────────────────


class TestPlanBlock:
    @pytest.mark.asyncio
    async def test_conflicts_marked_and_ranked(self, plugin):
        text = await _text(
            plugin,
            "plan_block",
            {
                "day": "Wednesday",
                "start": "13:00",
                "end": "17:00",
                "topics": ["AI", "agentic"],
            },
        )
        # Both AI sessions overlap 13:30-14:00 → mutual conflict markers.
        assert "AI-100" in text and "AI-101" in text
        assert "overlaps with: AI-101" in text
        assert "overlaps with: AI-100" in text
        # Water session (15:00) is in window but matches no topic → excluded.
        assert "Water Utility" not in text
        # Ranking: AI-101 matches both topics, AI-100 matches both too? AI-100
        # has 'agentic' in abstract and AI in title — both score 2; order by time.
        assert text.index("AI-100") < text.index("AI-101")
        assert "not a schedule" in text

    @pytest.mark.asyncio
    async def test_empty_topics_returns_all_in_window(self, plugin):
        result = await plugin.execute_tool(
            "plan_block",
            {"day": "wed", "start": "13:00", "end": "17:00", "topics": []},
        )
        assert not result.success  # topics is required non-empty
        assert "topics" in result.error_message

    @pytest.mark.asyncio
    async def test_end_before_start(self, plugin):
        result = await plugin.execute_tool(
            "plan_block",
            {"day": "wed", "start": "15:00", "end": "13:00", "topics": ["AI"]},
        )
        assert not result.success

    @pytest.mark.asyncio
    async def test_no_candidates(self, plugin):
        text = await _text(
            plugin,
            "plan_block",
            {"day": "thu", "start": "18:00", "end": "20:00", "topics": ["AI"]},
        )
        assert "No sessions" in text and "snapshot" in text


# ── find_exhibitors ────────────────────────────────────────────────────


class TestFindExhibitors:
    @pytest.mark.asyncio
    async def test_query(self, plugin):
        text = await _text(plugin, "find_exhibitors", {"query": "drone"})
        assert "Drone Dynamics — booth 715" in text
        assert "Acme" not in text

    @pytest.mark.asyncio
    async def test_category_filter(self, plugin):
        text = await _text(plugin, "find_exhibitors", {"category": "startup"})
        assert "Drone Dynamics" in text
        assert "Acme" not in text

    @pytest.mark.asyncio
    async def test_all(self, plugin):
        text = await _text(plugin, "find_exhibitors", {})
        assert "2 exhibitor(s) match" in text
        assert "1201, 1203" in text

    @pytest.mark.asyncio
    async def test_no_match(self, plugin):
        text = await _text(plugin, "find_exhibitors", {"query": "quantum blimp"})
        assert "No exhibitors match in the snapshot" in text


# ── where_is_room ──────────────────────────────────────────────────────


class TestWhereIsRoom:
    @pytest.mark.asyncio
    async def test_exact_room(self, plugin):
        text = await _text(plugin, "where_is_room", {"room_name": "Room 33 B"})
        assert "Room 33 B" in text
        assert "San Diego Convention Center (SDCC)" in text
        assert "Upper Level" in text
        assert "32.705714, -117.161211" in text
        assert "roomid=loc-33b" in text

    @pytest.mark.asyncio
    async def test_building_suffix_and_zero_padding(self, plugin):
        # RainFocus-style 'room | building' input resolves; numeric tokens
        # tolerate zero padding ('033' would be unusual, but '33' must match).
        text = await _text(plugin, "where_is_room", {"room_name": "Vista | Marriott"})
        assert "Marriott Marquis" in text

    @pytest.mark.asyncio
    async def test_no_reverse_containment(self, plugin):
        # 'Room 3' must NOT match 'Room 33 A/B' (the bug class this guards):
        # token '3' != '33'.
        text = await _text(plugin, "where_is_room", {"room_name": "Room 3"})
        assert "No rooms" in text

    @pytest.mark.asyncio
    async def test_ambiguous_lists_candidates(self, plugin):
        text = await _text(plugin, "where_is_room", {"room_name": "Room 33"})
        assert "2 rooms match" in text
        assert "Room 33 A | SDCC" in text

    @pytest.mark.asyncio
    async def test_missing_arg(self, plugin):
        result = await plugin.execute_tool("where_is_room", {"room_name": ""})
        assert not result.success

    @pytest.mark.asyncio
    async def test_tool_absent_without_rooms_data(self, tmp_path, data_files):
        cfg = dict(data_files)
        cfg["rooms_file"] = str(tmp_path / "absent-rooms.json")
        p = EsriUCPlugin(cfg)
        assert await p.initialize() is True
        assert "where_is_room" not in {t.name for t in p.get_tools()}
        result = await p.execute_tool("where_is_room", {"room_name": "Vista"})
        assert not result.success


# ── get_tracks_and_types ───────────────────────────────────────────────


class TestTracksAndTypes:
    @pytest.mark.asyncio
    async def test_vocabulary(self, plugin):
        text = await _text(plugin, "get_tracks_and_types")
        assert "Session types:" in text
        assert "Technical Session (3)" in text
        assert "Demo Theater Presentation (1)" in text
        assert "Living Atlas (1)" in text
        assert "Wednesday (2026-07-15)" in text
        assert "SDCC" in text and "Marriott" in text
        assert "Startup Zone Exhibitor (1)" in text


# ── Provenance on every response ───────────────────────────────────────


class TestProvenance:
    @pytest.mark.asyncio
    async def test_every_tool_response_carries_snapshot_date(self, plugin):
        calls = [
            ("search_sessions", {"query": "MCP"}),
            ("get_session", {"id_or_code": "AI-100"}),
            ("whats_on", {"at": "2026-07-15T13:15"}),
            (
                "plan_block",
                {"day": "wed", "start": "13:00", "end": "17:00", "topics": ["AI"]},
            ),
            ("find_exhibitors", {}),
            ("get_tracks_and_types", {}),
        ]
        for tool, args in calls:
            text = await _text(plugin, tool, args)
            assert "Schedule snapshot taken 2026-07-14" in text, tool
            assert "Esri Events app" in text, tool
