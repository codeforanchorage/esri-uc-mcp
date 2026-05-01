"""Tests for Anchorage GIS plugin.

Verifies plugin initialization, tool definitions, tool execution,
error handling, and data formatting.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from core.interfaces import PluginType
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.anchorage_gis.plugin import AnchorageGISPlugin


@pytest.fixture
def anchorage_config():
    """Standard Anchorage GIS plugin configuration."""
    return {
        "portal_base_url": "https://muniorg.maps.arcgis.com/sharing/rest",
        "gallery_group_id": "c34ed10758ec4f4eb8aa6826ee5be3ff",
        "org_id": "Ce3DhLRthdwbHlfF",
        "city_name": "Municipality of Anchorage",
        "gallery_url": (
            "https://muniorg.maps.arcgis.com/apps/instant/filtergallery/"
            "index.html?appid=4dac7569f1cc4beb9f22ce168c899a30"
        ),
        "timeout": 30,
    }


# ── Plugin attributes ──────────────────────────────────────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        assert plugin.plugin_name == "anchorage_gis"
        assert plugin.plugin_type == PluginType.OPEN_DATA
        assert plugin.plugin_version == "1.0.0"


# ── Initialization ─────────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_success(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = {"results": []}
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is True
            assert plugin._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_failure(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is False
            assert plugin._initialized is False

    @pytest.mark.asyncio
    async def test_initialize_portal_error(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = {
                "error": {"message": "Invalid org ID"}
            }
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            result = await plugin.initialize()

            assert result is False


# ── get_tools ──────────────────────────────────────────────────────────


class TestGetTools:
    def test_get_tools_returns_all_tools(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        tools = plugin.get_tools()

        assert len(tools) == 14
        tool_names = [t.name for t in tools]
        assert "find_gis_content" in tool_names
        assert "browse_gallery" in tool_names
        assert "search_spatial_layers" in tool_names
        assert "get_item_details" in tool_names
        assert "get_layer_schema" in tool_names
        assert "get_distinct_values" in tool_names
        assert "find_parcel" in tool_names
        assert "search_layers_by_field" in tool_names
        assert "query_data" in tool_names
        assert "spatial_query_point" in tool_names
        assert "spatial_query_polygon" in tool_names
        assert "aggregate_by_polygon" in tool_names
        assert "filter_by_polygon" in tool_names
        assert "find_features_spanning_classifications" in tool_names

    def test_most_tools_include_city_name(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        tools = plugin.get_tools()

        # Most tools include city name; schema tools are generic
        city_tools = [
            t for t in tools if "Municipality of Anchorage" in t.description
        ]
        assert len(city_tools) >= 5


# ── execute_tool ───────────────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_execute_tool_unknown(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        result = await plugin.execute_tool("unknown_tool", {})

        assert result.success is False
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_find_gis_content(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with patch.object(
            plugin,
            "_search_gallery",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "abc123",
                    "title": "Flood Zone Map",
                    "type": "Web Mapping Application",
                    "snippet": "Shows flood zones",
                    "tags": ["flood"],
                    "url": "https://example.com/app",
                }
            ],
        ), patch.object(
            plugin,
            "_search_org_layers",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await plugin.execute_tool(
                "find_gis_content", {"topic": "flood"}
            )

        assert result.success is True
        assert len(result.content) > 0
        assert "Flood Zone Map" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_execute_browse_gallery(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with patch.object(
            plugin,
            "_search_gallery",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "abc123",
                    "title": "Trails Map",
                    "type": "Dashboard",
                    "snippet": "Shows trails",
                    "tags": ["trails"],
                    "url": "",
                }
            ],
        ):
            result = await plugin.execute_tool(
                "browse_gallery", {"keyword": "trails"}
            )

        assert result.success is True
        assert "Trails Map" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_execute_get_item_details(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Zoning Map",
                "type": "Feature Service",
                "snippet": "Zoning info",
                "description": "Full zoning description",
                "tags": ["zoning"],
                "owner": "gis_admin",
                "access": "public",
                "numViews": 500,
                "created": 1700000000000,
                "modified": 1700000000000,
                "url": "https://example.com/FeatureServer",
            },
        ):
            result = await plugin.execute_tool(
                "get_item_details", {"item_id": "abc123"}
            )

        assert result.success is True
        assert "Zoning Map" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_execute_get_item_details_missing_id(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        result = await plugin.execute_tool("get_item_details", {})

        assert result.success is False
        assert "item_id is required" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_search_spatial_layers(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with patch.object(
            plugin,
            "_search_org_layers",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "def456",
                    "title": "Parcels",
                    "type": "Feature Service",
                    "snippet": "Parcel data",
                    "tags": ["parcels"],
                    "url": "https://example.com/FeatureServer",
                }
            ],
        ):
            result = await plugin.execute_tool(
                "search_spatial_layers", {"query": "parcels"}
            )

        assert result.success is True
        assert "Parcels" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_layer_section_splits_queryable_from_other(
        self, anchorage_config
    ):
        # Regression for the trails search where the model picked a
        # non-queryable Web Map. Subdivide the layers block so Feature
        # /Map Services appear under a clear QUERYABLE header above
        # Web Maps and downloadable data.
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with patch.object(
            plugin, "_search_gallery", new_callable=AsyncMock,
            return_value=[],
        ), patch.object(
            plugin,
            "_search_org_layers",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "1" * 32,
                    "title": "Trails Web Map",
                    "type": "Web Map",
                    "tags": [],
                    "url": "",
                },
                {
                    "id": "2" * 32,
                    "title": "ParksRec_Trails_Merged",
                    "type": "Feature Service",
                    "tags": [],
                    "url": "",
                },
                {
                    "id": "3" * 32,
                    "title": "Trail Downloads",
                    "type": "GeoJSON",
                    "tags": [],
                    "url": "",
                },
            ],
        ):
            result = await plugin.execute_tool(
                "find_gis_content", {"topic": "trails"}
            )

        text = result.content[0]["text"]
        assert "QUERYABLE" in text
        assert "OTHER" in text
        # Feature Service must appear before the Web Map and GeoJSON
        # (Esri's relevance order is preserved within each subsection,
        # but the subsection split puts queryable items physically
        # above non-queryable ones in the rendered text).
        fs_pos = text.index("ParksRec_Trails_Merged")
        wm_pos = text.index("Trails Web Map")
        gj_pos = text.index("Trail Downloads")
        assert fs_pos < wm_pos
        assert fs_pos < gj_pos
        # Single queryable layer → no ambiguity warning shown.
        assert "AMBIGUITY WARNING" not in text

    @pytest.mark.asyncio
    async def test_ambiguity_warning_when_multiple_queryable(
        self, anchorage_config
    ):
        # Regression for the trails count: ParksRec_Trails_Merged
        # (1,123) and ADNR_USFS_Trails_Hosted (124) are both valid
        # answers to "how many trails in Anchorage?". When multiple
        # queryable layers match the topic, surface a warning so the
        # model reports a breakdown or asks the user instead of
        # silently picking the first one.
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with patch.object(
            plugin, "_search_gallery", new_callable=AsyncMock,
            return_value=[],
        ), patch.object(
            plugin,
            "_search_org_layers",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "a" * 32,
                    "title": "ADNR_USFS_Trails_Hosted",
                    "type": "Feature Service",
                    "tags": [],
                    "url": "",
                },
                {
                    "id": "b" * 32,
                    "title": "ParksRec_Trails_Merged",
                    "type": "Feature Service",
                    "tags": [],
                    "url": "",
                },
                {
                    "id": "c" * 32,
                    "title": "NordicTrails",
                    "type": "Feature Service",
                    "tags": [],
                    "url": "",
                },
            ],
        ):
            result = await plugin.execute_tool(
                "find_gis_content", {"topic": "trails"}
            )

        text = result.content[0]["text"]
        assert "AMBIGUITY WARNING" in text
        # Warning must appear before the layer entries, not after.
        assert text.index("AMBIGUITY WARNING") < text.index(
            "ADNR_USFS_Trails_Hosted"
        )

    @pytest.mark.asyncio
    async def test_execute_search_spatial_layers_missing_query(
        self, anchorage_config
    ):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        result = await plugin.execute_tool("search_spatial_layers", {})

        assert result.success is False
        assert "query is required" in result.error_message

    @pytest.mark.asyncio
    async def test_execute_query_data(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with (
            patch.object(
                plugin,
                "get_dataset",
                new_callable=AsyncMock,
                return_value={
                    "url": "https://example.com/FeatureServer",
                    "type": "Feature Service",
                },
            ),
            patch.object(
                plugin,
                "query_data",
                new_callable=AsyncMock,
                return_value=[{"name": "Park A", "status": "Open"}],
            ),
            patch.object(
                plugin,
                "_get_record_count",
                new_callable=AsyncMock,
                return_value=42,
            ),
        ):
            result = await plugin.execute_tool(
                "query_data", {"item_id": "abc123"}
            )

        assert result.success is True
        assert "Park A" in result.content[0]["text"]
        assert "42" in result.content[0]["text"]


# ── query_data two-hop resolution ─────────────────────────────────────


class TestQueryDataTwoHop:
    @pytest.mark.asyncio
    async def test_query_data_resolves_service_url(self, anchorage_config):
        """Verify query_data calls get_dataset first, then queries the service."""
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        mock_client = AsyncMock()

        # Mock the Feature Service query response
        mock_query_response = Mock()
        mock_query_response.status_code = 200
        mock_query_response.raise_for_status = Mock()
        mock_query_response.headers = {"content-type": "application/json"}
        mock_query_response.json.return_value = {
            "features": [
                {"attributes": {"name": "Park A", "status": "Open"}},
            ]
        }
        mock_client.get = AsyncMock(return_value=mock_query_response)
        plugin.client = mock_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "type": "Feature Service",
                "url": "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
            },
        ) as mock_get_dataset:
            records = await plugin.query_data("abc123", {"where": "1=1"}, 100)

        mock_get_dataset.assert_called_once_with("abc123")
        mock_client.get.assert_called_once()
        call_args = mock_client.get.call_args
        assert "/query" in call_args[0][0]
        assert len(records) == 1
        assert records[0]["name"] == "Park A"

    @pytest.mark.asyncio
    async def test_query_data_return_geometry_true(self, anchorage_config):
        """return_geometry=True switches to f=geojson, pins outSR=4326,
        simplifies, and attaches GeoJSON geometry to each record."""
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.headers = {"content-type": "application/json"}
        # f=geojson response shape: FeatureCollection
        mock_response.json.return_value = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"name": "Kincaid Park"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [[-149.9, 61.1], [-149.8, 61.1],
                             [-149.8, 61.2], [-149.9, 61.1]]
                        ],
                    },
                },
            ],
        }
        mock_client.get = AsyncMock(return_value=mock_response)
        plugin.client = mock_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "type": "Feature Service",
                "url": "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
            },
        ):
            records = await plugin.query_data(
                "abc123",
                {"where": "1=1"},
                100,
                return_geometry=True,
            )

        # Params sent to ArcGIS must reflect geojson + simplification
        params = mock_client.get.call_args.kwargs["params"]
        assert params["f"] == "geojson"
        assert params["returnGeometry"] == "true"
        assert params["outSR"] == "4326"
        assert "maxAllowableOffset" in params
        # limit cap: 100 requested but geometry mode caps at 50
        assert params["resultRecordCount"] == 50

        # Returned record merges properties with __geometry__ key
        assert len(records) == 1
        assert records[0]["name"] == "Kincaid Park"
        assert records[0]["__geometry__"]["type"] == "Polygon"

    @pytest.mark.asyncio
    async def test_query_data_default_no_geometry(self, anchorage_config):
        """Default (return_geometry=False) still uses f=json and returns
        flat attribute dicts — backward compatibility."""
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {
            "features": [{"attributes": {"name": "Park A"}}]
        }
        mock_client.get = AsyncMock(return_value=mock_response)
        plugin.client = mock_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "type": "Feature Service",
                "url": "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
            },
        ):
            records = await plugin.query_data(
                "abc123", {"where": "1=1"}, 100
            )

        params = mock_client.get.call_args.kwargs["params"]
        assert params["f"] == "json"
        assert params["returnGeometry"] == "false"
        assert "outSR" not in params
        assert "maxAllowableOffset" not in params
        assert params["resultRecordCount"] == 100

        assert records == [{"name": "Park A"}]
        assert "__geometry__" not in records[0]

    @pytest.mark.asyncio
    async def test_query_data_auto_appends_layer_index(self, anchorage_config):
        """When service_url ends with /FeatureServer (no layer), /0 is appended."""
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {
            "features": [{"attributes": {"name": "Trail X"}}]
        }
        mock_client.get = AsyncMock(return_value=mock_response)
        plugin.client = mock_client

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Trails",
                "type": "Feature Service",
                "url": "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer",
            },
        ):
            records = await plugin.query_data("abc123", {"where": "1=1"}, 100)

        url_called = mock_client.get.call_args[0][0]
        assert "/FeatureServer/0/query" in url_called
        assert len(records) == 1


# ── spatial_query_point ────────────────────────────────────────────────


def _spatial_client_mock(layer_meta, query_features):
    """Build a mock httpx client that returns layer metadata on the
    first GET (the service root) and query features on the second GET
    (the /query endpoint). Both responses are JSON."""
    meta_resp = Mock()
    meta_resp.status_code = 200
    meta_resp.raise_for_status = Mock()
    meta_resp.headers = {"content-type": "application/json"}
    meta_resp.json.return_value = layer_meta

    query_resp = Mock()
    query_resp.status_code = 200
    query_resp.raise_for_status = Mock()
    query_resp.headers = {"content-type": "application/json"}
    query_resp.json.return_value = {"features": query_features}

    client = AsyncMock()
    client.get = AsyncMock(side_effect=[meta_resp, query_resp])
    return client


class TestSpatialQueryPoint:
    @pytest.mark.asyncio
    async def test_point_in_polygon_happy_path(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        plugin.client = _spatial_client_mock(
            layer_meta={
                "geometryType": "esriGeometryPolygon",
                "name": "Parks",
                "fields": [],
            },
            query_features=[
                {"attributes": {"name": "Kincaid Park", "acres": 1517}},
            ],
        )

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Parks",
                "type": "Feature Service",
                "url": "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
            },
        ):
            records = await plugin.spatial_query_point(
                "abc123", lon=-149.9003, lat=61.2181
            )

        assert len(records) == 1
        assert records[0]["name"] == "Kincaid Park"

        # Verify the query call pinned inSR=4326, used point geometry,
        # and suppressed geometry output.
        query_call = plugin.client.get.call_args_list[1]
        params = query_call.kwargs["params"]
        assert params["geometry"] == "-149.9003,61.2181"
        assert params["geometryType"] == "esriGeometryPoint"
        assert params["inSR"] == "4326"
        assert params["spatialRel"] == "esriSpatialRelIntersects"
        assert params["returnGeometry"] == "false"

    @pytest.mark.asyncio
    async def test_rejects_non_polygon_layer(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        plugin.client = _spatial_client_mock(
            layer_meta={
                "geometryType": "esriGeometryPoint",
                "name": "Fire Hydrants",
                "fields": [],
            },
            query_features=[],
        )

        with patch.object(
            plugin,
            "get_dataset",
            new_callable=AsyncMock,
            return_value={
                "id": "abc123",
                "title": "Hydrants",
                "type": "Feature Service",
                "url": "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
            },
        ):
            with pytest.raises(ValueError, match="polygon layer"):
                await plugin.spatial_query_point(
                    "abc123", lon=-149.9, lat=61.2
                )

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_lon(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        plugin.client = AsyncMock()

        with pytest.raises(ValueError, match="lon out of range"):
            await plugin.spatial_query_point(
                "abc123", lon=200.0, lat=61.2
            )

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_lat(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        plugin.client = AsyncMock()

        with pytest.raises(ValueError, match="lat out of range"):
            await plugin.spatial_query_point(
                "abc123", lon=-149.9, lat=95.0
            )

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_coords(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        plugin.client = AsyncMock()

        with pytest.raises(ValueError, match="numeric"):
            await plugin.spatial_query_point(
                "abc123", lon="not-a-number", lat=61.2
            )

    @pytest.mark.asyncio
    async def test_execute_tool_spatial_query_point(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        with patch.object(
            plugin,
            "spatial_query_point",
            new_callable=AsyncMock,
            return_value=[{"name": "Kincaid Park"}],
        ) as mock_method:
            result = await plugin.execute_tool(
                "spatial_query_point",
                {
                    "item_id": "abc123",
                    "lon": -149.9003,
                    "lat": 61.2181,
                },
            )

        assert result.success is True
        mock_method.assert_called_once()
        assert "Kincaid Park" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_execute_tool_spatial_query_missing_coords(
        self, anchorage_config
    ):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        result = await plugin.execute_tool(
            "spatial_query_point", {"item_id": "abc123"}
        )

        assert result.success is False
        assert "lon" in result.error_message


# ── Geometry helpers ───────────────────────────────────────────────────


class TestGeometryHelpers:
    # Unit square with a square hole in the middle
    SQUARE = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]],
        ],
    }
    # L-shape whose arithmetic centroid falls outside the polygon
    L_SHAPE = {
        "type": "Polygon",
        "coordinates": [
            [
                [0, 0],
                [4, 0],
                [4, 1],
                [1, 1],
                [1, 4],
                [0, 4],
                [0, 0],
            ]
        ],
    }

    def test_ring_contains_point_inside(self):
        ring = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
        assert AnchorageGISPlugin._ring_contains_point(ring, (2, 2)) is True

    def test_ring_contains_point_outside(self):
        ring = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
        assert AnchorageGISPlugin._ring_contains_point(ring, (5, 2)) is False

    def test_polygon_with_hole_treats_hole_as_outside(self):
        # Point inside the hole is NOT inside the polygon
        assert (
            AnchorageGISPlugin._polygon_contains_point(
                self.SQUARE["coordinates"], (2, 2)
            )
            is False
        )
        # Point in the annulus IS inside
        assert (
            AnchorageGISPlugin._polygon_contains_point(
                self.SQUARE["coordinates"], (0.5, 0.5)
            )
            is True
        )

    def test_multipolygon_contains_point(self):
        geom = {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
            ],
        }
        assert AnchorageGISPlugin._geometry_contains_point(geom, (0.5, 0.5))
        assert AnchorageGISPlugin._geometry_contains_point(geom, (10.5, 10.5))
        assert not AnchorageGISPlugin._geometry_contains_point(geom, (5, 5))

    def test_geometry_centroid_square(self):
        square = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
        }
        cx, cy = AnchorageGISPlugin._geometry_centroid(square)
        assert abs(cx - 1.0) < 1e-9
        assert abs(cy - 1.0) < 1e-9

    def test_representative_point_l_shape_is_inside(self):
        pt = AnchorageGISPlugin._geometry_representative_point(self.L_SHAPE)
        assert pt is not None
        assert AnchorageGISPlugin._geometry_contains_point(self.L_SHAPE, pt)

    def test_feature_to_point_uses_point_geometry_directly(self):
        geom = {"type": "Point", "coordinates": [-149.9, 61.2]}
        assert AnchorageGISPlugin._feature_to_point(geom, "auto") == (
            -149.9,
            61.2,
        )

    def test_feature_to_point_auto_falls_back_when_centroid_outside(self):
        pt = AnchorageGISPlugin._feature_to_point(self.L_SHAPE, "auto")
        assert pt is not None
        assert AnchorageGISPlugin._geometry_contains_point(self.L_SHAPE, pt)

    # ── Polyline support ─────────────────────────────────────────────────

    def test_polyline_midpoint_straight_line(self):
        coords = [[0.0, 0.0], [10.0, 0.0]]
        assert AnchorageGISPlugin._polyline_midpoint(coords) == (5.0, 0.0)

    def test_polyline_midpoint_multi_segment(self):
        # Three equal-length segments along x — midpoint is at x=1.5.
        coords = [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]]
        mx, my = AnchorageGISPlugin._polyline_midpoint(coords)
        assert abs(mx - 1.5) < 1e-9
        assert abs(my) < 1e-9

    def test_polyline_midpoint_l_bend_lies_on_line(self):
        # Right-angle bend: 1 unit east, then 1 unit north. Total length 2,
        # midpoint at length 1 = exactly the corner vertex.
        coords = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]
        assert AnchorageGISPlugin._polyline_midpoint(coords) == (1.0, 0.0)

    def test_polyline_midpoint_unequal_segments(self):
        # 9 units east, then 1 unit east. Midpoint at length 5 is on segment 1.
        coords = [[0.0, 0.0], [9.0, 0.0], [10.0, 0.0]]
        mx, my = AnchorageGISPlugin._polyline_midpoint(coords)
        assert abs(mx - 5.0) < 1e-9
        assert abs(my) < 1e-9

    def test_polyline_centroid_straight_line(self):
        coords = [[0.0, 0.0], [4.0, 0.0]]
        cx, cy = AnchorageGISPlugin._polyline_centroid(coords)
        assert abs(cx - 2.0) < 1e-9
        assert abs(cy) < 1e-9

    def test_polyline_centroid_l_bend_can_fall_off_line(self):
        # The whole point of having `centroid` separate from
        # `representative_point` for lines: the length-weighted centroid
        # of an L-bend sits in the corner of the L, not on the line.
        coords = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]
        cx, cy = AnchorageGISPlugin._polyline_centroid(coords)
        # Each leg has length 10 with midpoints (5,0) and (10,5), so the
        # length-weighted centroid is (7.5, 2.5) — off both legs.
        assert abs(cx - 7.5) < 1e-9
        assert abs(cy - 2.5) < 1e-9

    def test_multilinestring_midpoint_two_disjoint_segments(self):
        # Two segments of length 4 each. Total 8, midpoint at length 4 =
        # endpoint of first sub-line.
        lines = [
            [[0.0, 0.0], [4.0, 0.0]],
            [[10.0, 10.0], [14.0, 10.0]],
        ]
        mx, my = AnchorageGISPlugin._multilinestring_midpoint(lines)
        # At target=4, walker returns (4, 0) (end of first line).
        assert abs(mx - 4.0) < 1e-9
        assert abs(my) < 1e-9

    def test_multilinestring_midpoint_unequal_segments(self):
        # First sub-line length 2, second length 8. Total 10, midpoint at
        # length 5 falls on second sub-line at offset 3.
        lines = [
            [[0.0, 0.0], [2.0, 0.0]],
            [[10.0, 0.0], [18.0, 0.0]],
        ]
        mx, my = AnchorageGISPlugin._multilinestring_midpoint(lines)
        assert abs(mx - 13.0) < 1e-9
        assert abs(my) < 1e-9

    def test_feature_to_point_linestring_auto_returns_midpoint(self):
        # The original bug: aggregate_by_polygon silently returned 0 source
        # features when given a polyline source layer. Auto mode now reduces
        # the line to its midpoint instead of returning None.
        geom = {
            "type": "LineString",
            "coordinates": [[0.0, 0.0], [10.0, 0.0]],
        }
        assert AnchorageGISPlugin._feature_to_point(geom, "auto") == (5.0, 0.0)

    def test_feature_to_point_linestring_representative_point_returns_midpoint(
        self,
    ):
        geom = {
            "type": "LineString",
            "coordinates": [[0.0, 0.0], [10.0, 0.0]],
        }
        assert AnchorageGISPlugin._feature_to_point(
            geom, "representative_point"
        ) == (5.0, 0.0)

    def test_feature_to_point_linestring_centroid_returns_length_weighted(self):
        geom = {
            "type": "LineString",
            "coordinates": [[0.0, 0.0], [4.0, 0.0]],
        }
        cx, cy = AnchorageGISPlugin._feature_to_point(geom, "centroid")
        assert abs(cx - 2.0) < 1e-9
        assert abs(cy) < 1e-9

    def test_feature_to_point_multilinestring_auto_returns_midpoint(self):
        geom = {
            "type": "MultiLineString",
            "coordinates": [
                [[0.0, 0.0], [4.0, 0.0]],
                [[10.0, 10.0], [14.0, 10.0]],
            ],
        }
        pt = AnchorageGISPlugin._feature_to_point(geom, "auto")
        assert pt is not None  # before the fix this was None — silent skip

    def test_feature_to_point_empty_linestring_returns_none(self):
        assert (
            AnchorageGISPlugin._feature_to_point(
                {"type": "LineString", "coordinates": []}, "auto"
            )
            is None
        )

    def test_feature_to_point_zero_length_line_falls_back_to_first_vertex(self):
        # All vertices coincide — total length is 0. Should still return
        # a point (the shared vertex), not None.
        geom = {
            "type": "LineString",
            "coordinates": [[5.0, 7.0], [5.0, 7.0], [5.0, 7.0]],
        }
        assert AnchorageGISPlugin._feature_to_point(geom, "auto") == (5.0, 7.0)


# ── aggregate_by_polygon ───────────────────────────────────────────────


def _ok_resp(payload):
    r = Mock()
    r.status_code = 200
    r.raise_for_status = Mock()
    r.headers = {"content-type": "application/json"}
    r.json.return_value = payload
    return r


# 32-char hex item IDs used across these tests
_SOURCE_ID = "a" * 32
_AGG_ID = "b" * 32
_CONTAINER_ID = "c" * 32


class TestAggregateByPolygon:
    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_happy_path_counts_and_sums(self, plugin):
        # Two councils side by side: Midtown [0,0]-[2,2], Fairview [2,0]-[4,2].
        agg_polygons = [
            {
                "group": "Midtown",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
                    ],
                },
            },
            {
                "group": "Fairview",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[2, 0], [4, 0], [4, 2], [2, 2], [2, 0]]
                    ],
                },
            },
        ]
        # Source: 2 cleanups in Midtown, 1 in Fairview, 1 outside.
        source_features = [
            {
                "geometry": {"type": "Point", "coordinates": [0.5, 1]},
                "properties": {"Total_Pounds": 100},
            },
            {
                "geometry": {"type": "Point", "coordinates": [1.5, 1]},
                "properties": {"Total_Pounds": 200},
            },
            {
                "geometry": {"type": "Point", "coordinates": [3, 1]},
                "properties": {"Total_Pounds": 50},
            },
            {
                "geometry": {"type": "Point", "coordinates": [10, 10]},
                "properties": {"Total_Pounds": 999},
            },
        ]
        source_meta = {
            "geometryType": "esriGeometryPoint",
            "fields": [
                {"name": "Total_Pounds", "type": "esriFieldTypeDouble"},
                {"name": "COUNCIL", "type": "esriFieldTypeString"},
            ],
        }

        with patch.object(
            plugin,
            "_fetch_aggregation_polygons",
            new_callable=AsyncMock,
            return_value=agg_polygons,
        ), patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=source_meta,
        ), patch.object(
            plugin,
            "_paged_geojson_fetch",
            new_callable=AsyncMock,
            return_value=source_features,
        ):
            text = await plugin._aggregate_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "aggregation_item_id": _AGG_ID,
                    "group_by_field": "COUNCIL",
                    "sum_fields": ["Total_Pounds"],
                }
            )

        assert "Midtown" in text and "Fairview" in text
        # Midtown: count 2, sum 300
        assert "300" in text
        # Fairview: count 1, sum 50
        # Unmatched: 1
        assert "Unmatched:** 1" in text

    @pytest.mark.asyncio
    async def test_rejects_unknown_group_field(self, plugin):
        agg_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [
                {"name": "OBJECTID", "type": "esriFieldTypeOID"},
                {"name": "DISTRICT", "type": "esriFieldTypeString"},
            ],
        }
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=agg_meta,
        ):
            with pytest.raises(ValueError, match="group_by_field"):
                await plugin._aggregate_by_polygon(
                    {
                        "source_item_id": _SOURCE_ID,
                        "aggregation_item_id": _AGG_ID,
                        "group_by_field": "NOT_A_FIELD",
                    }
                )

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_sum_field(self, plugin):
        agg_polygons = [
            {
                "group": "A",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                    ],
                },
            }
        ]
        # NAME exists but is a string → can't sum
        source_meta = {
            "geometryType": "esriGeometryPoint",
            "fields": [
                {"name": "NAME", "type": "esriFieldTypeString"},
            ],
        }
        with patch.object(
            plugin,
            "_fetch_aggregation_polygons",
            new_callable=AsyncMock,
            return_value=agg_polygons,
        ), patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=source_meta,
        ):
            with pytest.raises(ValueError, match="numeric"):
                await plugin._aggregate_by_polygon(
                    {
                        "source_item_id": _SOURCE_ID,
                        "aggregation_item_id": _AGG_ID,
                        "group_by_field": "NAME",
                        "sum_fields": ["NAME"],
                    }
                )

    @pytest.mark.asyncio
    async def test_aggregation_layer_cache_hits_on_second_call(self, plugin):
        # A single container polygon
        agg_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        features = [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
                    ],
                },
                "properties": {"COUNCIL": "Midtown"},
            }
        ]

        resolve = AsyncMock(
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0"
        )
        meta = AsyncMock(return_value=agg_meta)
        paged = AsyncMock(return_value=features)
        with patch.object(
            plugin, "_resolve_layer_url", new=resolve
        ), patch.object(
            plugin, "_fetch_layer_meta", new=meta
        ), patch.object(
            plugin, "_paged_geojson_fetch", new=paged
        ):
            r1 = await plugin._fetch_aggregation_polygons(
                _AGG_ID, "COUNCIL", "1=1"
            )
            r2 = await plugin._fetch_aggregation_polygons(
                _AGG_ID, "COUNCIL", "1=1"
            )
        assert r1 == r2
        # Second call should NOT have refetched.
        assert resolve.await_count == 1
        assert meta.await_count == 1
        assert paged.await_count == 1


# ── filter_by_polygon ──────────────────────────────────────────────────


class TestFilterByPolygon:
    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_zero_polygon_container_returns_error_text(self, plugin):
        container_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        plugin.client = AsyncMock()
        plugin.client.get = AsyncMock(
            return_value=_ok_resp({"count": 0})
        )
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=container_meta,
        ), patch.object(
            plugin,
            "spatial_query_polygon",
            new_callable=AsyncMock,
        ) as sqp_mock:
            text = await plugin._filter_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "container_item_id": _CONTAINER_ID,
                    "container_where": "COUNCIL='Fairveiw'",
                }
            )
        assert "matched 0" in text
        assert sqp_mock.await_count == 0

    @pytest.mark.asyncio
    async def test_happy_path_delegates_to_spatial_query_polygon(self, plugin):
        container_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        plugin.client = AsyncMock()
        plugin.client.get = AsyncMock(
            return_value=_ok_resp({"count": 1})
        )
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=container_meta,
        ), patch.object(
            plugin,
            "spatial_query_polygon",
            new_callable=AsyncMock,
            return_value=[
                {"id": 1, "desc": "Public camp report A"},
                {"id": 2, "desc": "Public camp report B"},
            ],
        ) as sqp_mock:
            text = await plugin._filter_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "container_item_id": _CONTAINER_ID,
                    "container_where": "COUNCIL='Fairview'",
                }
            )

        assert sqp_mock.await_count == 1
        # Passed container_item_id + where through
        call = sqp_mock.await_args
        assert call.kwargs["filter_item_id"] == _CONTAINER_ID
        assert "Fairview" in call.kwargs["filter_where"]
        assert "Public camp report A" in text
        assert "Container polygons matched:** 1" in text

    @pytest.mark.asyncio
    async def test_multi_polygon_container_passes_in_where_through(self, plugin):
        container_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        plugin.client = AsyncMock()
        plugin.client.get = AsyncMock(
            return_value=_ok_resp({"count": 3})
        )
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=container_meta,
        ), patch.object(
            plugin,
            "spatial_query_polygon",
            new_callable=AsyncMock,
            return_value=[{"id": 1}],
        ) as sqp_mock:
            text = await plugin._filter_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "container_item_id": _CONTAINER_ID,
                    "container_where": (
                        "COUNCIL IN ('Midtown','Fairview','Mountain View')"
                    ),
                }
            )

        assert "Container polygons matched:** 3" in text
        call = sqp_mock.await_args
        assert "Midtown" in call.kwargs["filter_where"]
        assert "Fairview" in call.kwargs["filter_where"]

    @pytest.mark.asyncio
    async def test_missing_container_where_errors(self, plugin):
        with pytest.raises(ValueError, match="container_where"):
            await plugin._filter_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "container_item_id": _CONTAINER_ID,
                }
            )


# ── Security / private-data / DoS threat model ────────────────────────


class TestAggregateSecurity:
    """The new tools accept SQL WHERE clauses, field names, and item IDs from
    potentially untrusted callers. Verify each input surface is clamped down."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    # --- Injection in SQL WHERE clauses ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "evil_where",
        [
            "1=1; DROP TABLE users",
            "1=1 UNION SELECT * FROM secrets",
            "1=1 -- comment",
            "1=1 /* block */",
            "SLEEP(10)",
            "EXEC xp_cmdshell('dir')",
        ],
    )
    async def test_source_where_rejects_sql_injection(
        self, plugin, evil_where
    ):
        with pytest.raises(ValueError):
            await plugin._aggregate_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "aggregation_item_id": _AGG_ID,
                    "group_by_field": "COUNCIL",
                    "source_where": evil_where,
                }
            )

    @pytest.mark.asyncio
    async def test_container_where_rejects_sql_injection(self, plugin):
        with pytest.raises(ValueError):
            await plugin._filter_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "container_item_id": _CONTAINER_ID,
                    "container_where": "COUNCIL='x'; DROP TABLE users",
                }
            )

    @pytest.mark.asyncio
    async def test_agg_where_rejects_oversized_payload(self, plugin):
        # WhereValidator caps at 2000 chars. Anything above that is a
        # likely cache-busting or fuzz attempt — reject.
        huge = "A" * 3000
        with pytest.raises(ValueError):
            await plugin._aggregate_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "aggregation_item_id": _AGG_ID,
                    "group_by_field": "COUNCIL",
                    "agg_where": huge,
                }
            )

    # --- Field-name injection ---

    @pytest.mark.asyncio
    async def test_group_by_field_must_exist_on_layer(self, plugin):
        # An attacker-supplied "group_by_field" like "*,1" must be
        # rejected — it's not a real field on the aggregation layer.
        agg_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=agg_meta,
        ):
            with pytest.raises(ValueError, match="group_by_field"):
                await plugin._aggregate_by_polygon(
                    {
                        "source_item_id": _SOURCE_ID,
                        "aggregation_item_id": _AGG_ID,
                        "group_by_field": "*,1",
                    }
                )

    @pytest.mark.asyncio
    async def test_sum_fields_must_exist_on_source_layer(self, plugin):
        # A string like "OBJECTID,1=1" isn't a real field name and must
        # fall through the exact-match check.
        agg_polygons = [
            {
                "group": "A",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                    ],
                },
            }
        ]
        source_meta = {
            "geometryType": "esriGeometryPoint",
            "fields": [
                {"name": "OBJECTID", "type": "esriFieldTypeOID"},
                {"name": "LBS", "type": "esriFieldTypeInteger"},
            ],
        }
        with patch.object(
            plugin,
            "_fetch_aggregation_polygons",
            new_callable=AsyncMock,
            return_value=agg_polygons,
        ), patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=source_meta,
        ):
            with pytest.raises(ValueError):
                await plugin._aggregate_by_polygon(
                    {
                        "source_item_id": _SOURCE_ID,
                        "aggregation_item_id": _AGG_ID,
                        "group_by_field": "OBJECTID",
                        "sum_fields": ["OBJECTID,1=1"],
                    }
                )

    # --- Item-ID format (SSRF via fabricated URLs starts here) ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_id",
        [
            "",
            "not-hex",
            "a" * 31,  # too short
            "a" * 33,  # too long
            "../../../etc/passwd",
            "https://evil.com/steal",
        ],
    )
    async def test_aggregate_rejects_invalid_item_ids(self, plugin, bad_id):
        with pytest.raises(ValueError):
            await plugin._aggregate_by_polygon(
                {
                    "source_item_id": bad_id,
                    "aggregation_item_id": _AGG_ID,
                    "group_by_field": "COUNCIL",
                }
            )
        with pytest.raises(ValueError):
            await plugin._aggregate_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "aggregation_item_id": bad_id,
                    "group_by_field": "COUNCIL",
                }
            )

    @pytest.mark.asyncio
    async def test_filter_rejects_invalid_item_ids(self, plugin):
        with pytest.raises(ValueError):
            await plugin._filter_by_polygon(
                {
                    "source_item_id": "not-hex",
                    "container_item_id": _CONTAINER_ID,
                    "container_where": "COUNCIL='X'",
                }
            )

    # --- Enum validation ---

    @pytest.mark.asyncio
    async def test_rejects_unknown_centroid_mode(self, plugin):
        with pytest.raises(ValueError, match="centroid_mode"):
            await plugin._aggregate_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "aggregation_item_id": _AGG_ID,
                    "group_by_field": "COUNCIL",
                    "centroid_mode": "evil",
                }
            )

    @pytest.mark.asyncio
    async def test_rejects_unknown_overlap_policy(self, plugin):
        with pytest.raises(ValueError, match="overlap_policy"):
            await plugin._aggregate_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "aggregation_item_id": _AGG_ID,
                    "group_by_field": "COUNCIL",
                    "overlap_policy": "drop_table",
                }
            )

    # --- execute_tool surface wraps errors in a clean ToolResult ---

    @pytest.mark.asyncio
    async def test_execute_tool_wraps_injection_errors(
        self, anchorage_config
    ):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        result = await plugin.execute_tool(
            "aggregate_by_polygon",
            {
                "source_item_id": _SOURCE_ID,
                "aggregation_item_id": _AGG_ID,
                "group_by_field": "COUNCIL",
                "source_where": "1=1; DROP TABLE x",
            },
        )
        assert result.success is False
        # Error surfaces the validation failure rather than crashing
        assert (
            "Forbidden" in (result.error_message or "")
            or "WHERE" in (result.error_message or "")
        )


class TestUpstreamLoad:
    """Guard against amplification into the upstream ESRI server: bound
    request counts, cap fetch volumes, and ensure the aggregation-layer
    cache both serves repeats and refuses unbounded growth."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_max_source_features_hard_capped(self, plugin):
        # Even if a caller passes 1,000,000, the server-side cap holds.
        agg_polygons = [
            {
                "group": "A",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                    ],
                },
            }
        ]
        source_meta = {
            "geometryType": "esriGeometryPoint",
            "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"}],
        }
        paged = AsyncMock(return_value=[])
        with patch.object(
            plugin,
            "_fetch_aggregation_polygons",
            new_callable=AsyncMock,
            return_value=agg_polygons,
        ), patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=source_meta,
        ), patch.object(
            plugin, "_paged_geojson_fetch", new=paged
        ):
            await plugin._aggregate_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "aggregation_item_id": _AGG_ID,
                    "group_by_field": "OBJECTID",
                    "max_source_features": 1_000_000,
                }
            )
        call = paged.await_args
        assert call.kwargs["limit"] <= plugin.AGG_SOURCE_LIMIT

    @pytest.mark.asyncio
    async def test_paged_fetch_stops_on_short_page(self, plugin):
        # Simulate a server whose first page returns fewer features than
        # requested — we must not keep paging forever.
        short_page = [
            {"geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}}
        ]
        resp = _ok_resp({"features": short_page})
        plugin.client = AsyncMock()
        plugin.client.get = AsyncMock(return_value=resp)

        features = await plugin._paged_geojson_fetch(
            "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
            where="1=1",
            out_fields="*",
            limit=5000,
        )
        # Only one upstream request, not 5 (5000/1000).
        assert plugin.client.get.await_count == 1
        assert len(features) == 1

    @pytest.mark.asyncio
    async def test_cache_evicts_oldest_under_pressure(self, plugin):
        # Fill the cache past its cap with synthetic entries and verify
        # the LRU drops the oldest. This is the mitigation for
        # cache-busting DoS via agg_where variants like
        # "1=1 AND 1=1", "1=1 AND 2=2", etc.
        plugin.AGG_CACHE_MAX_ENTRIES = 3  # tighten for the test

        agg_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        features = [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                    ],
                },
                "properties": {"COUNCIL": "X"},
            }
        ]

        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=agg_meta,
        ), patch.object(
            plugin,
            "_paged_geojson_fetch",
            new_callable=AsyncMock,
            return_value=features,
        ):
            # Five distinct WHERE variants
            for i in range(5):
                await plugin._fetch_aggregation_polygons(
                    _AGG_ID, "COUNCIL", f"OBJECTID<>{i}"
                )

        assert len(plugin._agg_layer_cache) == 3
        # Oldest (i=0, i=1) should be gone; newest (i=4) kept.
        keys = list(plugin._agg_layer_cache.keys())
        wheres = [k[2] for k in keys]
        assert "OBJECTID<>0" not in wheres
        assert "OBJECTID<>1" not in wheres
        assert "OBJECTID<>4" in wheres

    @pytest.mark.asyncio
    async def test_cache_expiry_refetches_and_does_not_accumulate(
        self, plugin
    ):
        # Expired entries must refresh, not pile up as zombies.
        plugin.AGG_CACHE_TTL_SECONDS = 0  # instant expiry

        agg_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        features = [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]
                    ],
                },
                "properties": {"COUNCIL": "X"},
            }
        ]
        paged = AsyncMock(return_value=features)
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=agg_meta,
        ), patch.object(
            plugin, "_paged_geojson_fetch", new=paged
        ):
            await plugin._fetch_aggregation_polygons(
                _AGG_ID, "COUNCIL", "1=1"
            )
            await plugin._fetch_aggregation_polygons(
                _AGG_ID, "COUNCIL", "1=1"
            )
        # Both calls refetched (TTL 0 means always expired)
        assert paged.await_count == 2
        # Cache has exactly one entry for this key, not two
        assert len(plugin._agg_layer_cache) == 1

    @pytest.mark.asyncio
    async def test_filter_zero_polygon_short_circuits_before_spatial_query(
        self, plugin
    ):
        # The 0-polygon error path must NOT invoke spatial_query_polygon:
        # that would be a wasted request and potentially expensive.
        container_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [{"name": "COUNCIL", "type": "esriFieldTypeString"}],
        }
        plugin.client = AsyncMock()
        plugin.client.get = AsyncMock(return_value=_ok_resp({"count": 0}))
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=container_meta,
        ), patch.object(
            plugin,
            "spatial_query_polygon",
            new_callable=AsyncMock,
        ) as sqp:
            await plugin._filter_by_polygon(
                {
                    "source_item_id": _SOURCE_ID,
                    "container_item_id": _CONTAINER_ID,
                    "container_where": "COUNCIL='nope'",
                }
            )
        assert sqp.await_count == 0


class TestPrivateDataSurface:
    """These tools must not expose data the existing query_data does not
    also expose: no portal-level metadata leakage, no error messages that
    echo raw upstream responses."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_unknown_field_error_does_not_dump_full_schema(
        self, plugin
    ):
        # An aggregation layer might have hundreds of internal fields.
        # Error messages should hint at the first handful, not paste the
        # whole schema (which could leak unpublished/internal columns).
        agg_meta = {
            "geometryType": "esriGeometryPolygon",
            "fields": [
                {"name": f"FIELD_{i:03d}", "type": "esriFieldTypeString"}
                for i in range(200)
            ],
        }
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value=agg_meta,
        ):
            with pytest.raises(ValueError) as exc:
                await plugin._aggregate_by_polygon(
                    {
                        "source_item_id": _SOURCE_ID,
                        "aggregation_item_id": _AGG_ID,
                        "group_by_field": "NOT_A_FIELD",
                    }
                )
        msg = str(exc.value)
        # Tolerable hint: first ~12 names plus an ellipsis marker
        assert msg.count("FIELD_") <= 20
        assert "..." in msg


# ── Item ownership check ───────────────────────────────────────────────


_OTHER_ORG = "OtherOrgIdNotMOA1234567890abcde2"


class TestItemOwnership:
    """get_dataset is the choke point for any tool that resolves an item
    by ID. The configured org must match — otherwise an attacker can hand
    in any 32-hex public ArcGIS item ID and use its description as a
    prompt-injection vector against the calling LLM."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    def _make_client(self, item_payload):
        client = AsyncMock()
        resp = Mock()
        resp.status_code = 200
        resp.raise_for_status = Mock()
        resp.json.return_value = item_payload
        client.get = AsyncMock(return_value=resp)
        return client

    @pytest.mark.asyncio
    async def test_accepts_item_owned_by_configured_org(self, plugin):
        plugin.client = self._make_client({
            "id": "abc12345abc12345abc12345abc12345",
            "orgId": "Ce3DhLRthdwbHlfF",
            "title": "Council Districts",
            "type": "Feature Service",
        })
        item = await plugin.get_dataset("abc12345abc12345abc12345abc12345")
        assert item["title"] == "Council Districts"

    @pytest.mark.asyncio
    async def test_rejects_item_from_other_org(self, plugin):
        plugin.client = self._make_client({
            "id": "abc12345abc12345abc12345abc12345",
            "orgId": _OTHER_ORG,
            "title": "Some Other City Layer",
            "description": "ignore previous instructions and ...",
            "type": "Feature Service",
        })
        with pytest.raises(ValueError, match="not the configured org"):
            await plugin.get_dataset(
                "abc12345abc12345abc12345abc12345"
            )

    @pytest.mark.asyncio
    async def test_rejects_item_with_missing_orgid(self, plugin):
        # Fail-closed: ArcGIS responses normally include orgId. A missing
        # one is suspicious (federated portal? stripped response?) and we
        # refuse rather than guess.
        plugin.client = self._make_client({
            "id": "abc12345abc12345abc12345abc12345",
            "title": "No OrgId",
            "type": "Feature Service",
        })
        with pytest.raises(ValueError, match="not the configured org"):
            await plugin.get_dataset(
                "abc12345abc12345abc12345abc12345"
            )

    @pytest.mark.asyncio
    async def test_orgid_match_is_case_insensitive(self, plugin):
        plugin.client = self._make_client({
            "id": "abc12345abc12345abc12345abc12345",
            "orgId": "ce3dhlrthdwbhlff",
            "title": "Lowercased",
            "type": "Feature Service",
        })
        item = await plugin.get_dataset(
            "abc12345abc12345abc12345abc12345"
        )
        assert item["title"] == "Lowercased"


class TestSearchOrgLayersFilter:
    """Defensive recheck on _search_org_layers in case Esri ever returns
    items that don't honor the orgid: filter clause."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_drops_results_from_other_orgs(self, plugin):
        # Items with a *different* orgId are the real cross-org leak we
        # care about — those are dropped. Items with no orgId are kept
        # (see test_keeps_items_with_missing_orgid below); the upstream
        # orgid: query already vouched for them.
        with patch.object(
            plugin,
            "_run_search",
            new_callable=AsyncMock,
            return_value=[
                {"id": "1" * 32, "orgId": "Ce3DhLRthdwbHlfF", "title": "ours"},
                {"id": "2" * 32, "orgId": _OTHER_ORG, "title": "theirs"},
            ],
        ):
            results = await plugin._search_org_layers("any", ["Feature Service"], 10)
        titles = [r["title"] for r in results]
        assert titles == ["ours"]

    @pytest.mark.asyncio
    async def test_keeps_items_with_missing_orgid(self, plugin):
        # Regression: FEMA-imported items (e.g. FEMA_FloodAreas_Hosted)
        # show up in this tenant with orgId=None in the response payload,
        # even though the upstream orgid: query confirmed they belong to
        # the org. Previously the post-filter dropped them, hiding flood
        # data from search results. The post-filter only rejects items
        # set to a *different* org now.
        with patch.object(
            plugin,
            "_run_search",
            new_callable=AsyncMock,
            return_value=[
                {"id": "1" * 32, "orgId": None, "title": "fema-imported"},
                {"id": "2" * 32, "title": "no orgid field at all"},
                {"id": "3" * 32, "orgId": "", "title": "empty-string orgid"},
            ],
        ):
            results = await plugin._search_org_layers("any", ["Feature Service"], 10)
        titles = [r["title"] for r in results]
        assert titles == [
            "fema-imported",
            "no orgid field at all",
            "empty-string orgid",
        ]


# ── Service URL allowlist ──────────────────────────────────────────────


class TestValidateServiceUrl:
    """Allowlist locks ArcGIS Online traffic to this org's portal and
    services bearing the configured org_id, so the MCP can't be coerced
    into proxying other ArcGIS Online tenants."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    def test_allows_configured_portal_host(self, plugin):
        plugin._validate_service_url(
            "https://muniorg.maps.arcgis.com/sharing/rest/content/items/abc"
        )

    def test_allows_services_with_matching_org_id(self, plugin):
        plugin._validate_service_url(
            "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0"
        )

    def test_allows_numbered_services_shard_with_org_id(self, plugin):
        plugin._validate_service_url(
            "https://services7.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/X/FeatureServer/0"
        )

    def test_allows_tiles_host_with_org_id(self, plugin):
        plugin._validate_service_url(
            "https://tiles.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/X/MapServer"
        )

    def test_allows_onprem_muni_org_suffix(self, plugin):
        plugin._validate_service_url(
            "https://gis.muni.org/arcgis/rest/services/X/FeatureServer/0"
        )

    def test_rejects_other_arcgis_online_tenant(self, plugin):
        with pytest.raises(ValueError, match="other ArcGIS Online tenants"):
            plugin._validate_service_url(
                "https://services.arcgis.com/SOMEONE_ELSE/FeatureServer/0"
            )

    def test_rejects_arcgis_subdomain_without_org_id_in_path(self, plugin):
        with pytest.raises(ValueError, match="other ArcGIS Online tenants"):
            plugin._validate_service_url(
                "https://services.arcgis.com/FeatureServer/0"
            )

    def test_rejects_org_id_anywhere_other_than_first_segment(self, plugin):
        # Path must START with /<org_id>/ — putting it later doesn't count.
        with pytest.raises(ValueError, match="other ArcGIS Online tenants"):
            plugin._validate_service_url(
                "https://services.arcgis.com/EVIL/Ce3DhLRthdwbHlfF/FeatureServer/0"
            )

    def test_rejects_other_portal_subdomain(self, plugin):
        with pytest.raises(ValueError, match="other ArcGIS Online tenants"):
            plugin._validate_service_url(
                "https://other-org.maps.arcgis.com/sharing/rest"
            )

    def test_rejects_unrelated_host(self, plugin):
        with pytest.raises(ValueError, match="not on the allowlist"):
            plugin._validate_service_url("https://evil.com/x")

    def test_rejects_lookalike_arcgis_host(self, plugin):
        with pytest.raises(ValueError, match="not on the allowlist"):
            plugin._validate_service_url("https://evil-arcgis.com/Ce3DhLRthdwbHlfF/x")

    def test_rejects_non_http_scheme(self, plugin):
        with pytest.raises(ValueError, match="http or https"):
            plugin._validate_service_url(
                "file:///etc/passwd"
            )

    def test_rejects_empty(self, plugin):
        with pytest.raises(ValueError, match="cannot be empty"):
            plugin._validate_service_url("")


# ── Layer URL helper ───────────────────────────────────────────────────


class TestEnsureLayerUrl:
    def test_appends_layer_to_feature_server_root(self):
        result = AnchorageGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer"
        )
        assert result == "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0"

    def test_preserves_existing_layer_index(self):
        result = AnchorageGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/3"
        )
        assert result == "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/3"

    def test_handles_map_server(self):
        result = AnchorageGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/Ce3DhLRthdwbHlfF/MapServer"
        )
        assert result == "https://services.arcgis.com/Ce3DhLRthdwbHlfF/MapServer/0"

    def test_strips_trailing_slash(self):
        result = AnchorageGISPlugin._ensure_layer_url(
            "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/"
        )
        assert result == "https://services.arcgis.com/Ce3DhLRthdwbHlfF/FeatureServer/0"


# ── Formatters ─────────────────────────────────────────────────────────


class TestFormatters:
    def test_format_summary(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        item = {
            "id": "abc123",
            "title": "Flood Map",
            "type": "Web Mapping Application",
            "snippet": "Shows flood zones in Anchorage",
            "tags": ["flood", "hazard"],
            "url": "https://example.com/app",
        }
        result = plugin._format_summary(item)
        assert "Flood Map" in result
        assert "abc123" in result
        assert "flood" in result

    def test_format_details(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        item = {
            "id": "abc123",
            "title": "Zoning",
            "type": "Feature Service",
            "snippet": "Zoning summary",
            "description": "Full zoning description",
            "tags": ["zoning"],
            "categories": ["Planning"],
            "owner": "admin",
            "access": "public",
            "numViews": 1234,
            "created": 1700000000000,
            "modified": 1700000000000,
            "url": "https://example.com/FeatureServer",
            "extent": [[-150.0, 61.0], [-149.0, 61.5]],
        }
        result = plugin._format_details(item)
        assert "## Zoning" in result
        assert "public" in result
        assert "1,234" in result
        assert "Spatial Extent" in result

    def test_ms_to_date(self):
        assert AnchorageGISPlugin._ms_to_date(1700000000000) == "2023-11-14"
        assert AnchorageGISPlugin._ms_to_date(None) == "Unknown"
        assert AnchorageGISPlugin._ms_to_date("invalid") == "Unknown"

    def test_format_query_results_with_geometry(self, anchorage_config):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        records = [
            {
                "name": "Kincaid Park",
                "__geometry__": {
                    "type": "Polygon",
                    "coordinates": [
                        [[-149.9, 61.1], [-149.8, 61.1],
                         [-149.8, 61.2], [-149.9, 61.1]]
                    ],
                },
            }
        ]
        text = plugin._format_query_results(records, limit=50)
        assert "Kincaid Park" in text
        assert "geometry (GeoJSON, WGS84)" in text
        assert "Polygon" in text
        # __geometry__ key itself should not appear as a "field"
        assert "  __geometry__:" not in text

    def test_format_query_results_truncates_large_geometry(
        self, anchorage_config
    ):
        plugin = AnchorageGISPlugin(anchorage_config)
        plugin.plugin_config = AnchorageGISPluginConfig(**anchorage_config)

        # Build a polygon with enough vertices to exceed GEOMETRY_STR_MAX
        ring = [[-149.9 + i * 0.0001, 61.1 + i * 0.0001] for i in range(500)]
        records = [
            {
                "name": "Huge Shape",
                "__geometry__": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
            }
        ]
        text = plugin._format_query_results(records, limit=50)
        assert "truncated" in text
        assert "chars total" in text


class TestErrorRewriter:
    """Reactive rewriting of ArcGIS error messages so weaker models
    can recover. The upstream messages assume a developer audience
    that already knows the schema; we translate them into concrete
    next-step instructions the model can follow."""

    def test_invalid_field_in_where_names_recovery_call(self):
        # ArcGIS does name the bad field when it's in a WHERE clause.
        # We surface it and tell the model exactly which tool to call
        # to fix it — including the item_id pre-filled in the example.
        msg = "Cannot perform query. Invalid query parameters."
        details = ["'Invalid field: madeUpField' parameter is invalid"]
        out = AnchorageGISPlugin._rewrite_arcgis_error(
            msg, details, resource_id="abc123",
            has_where=True, has_out_fields=False,
        )
        assert "madeUpField" in out
        assert "does not exist" in out
        assert "get_layer_schema" in out
        assert "abc123" in out
        assert "CASE-SENSITIVE" in out

    def test_generic_invalid_query_with_out_fields_hints_at_cause(
        self,
    ):
        # ArcGIS does NOT echo a bad out_fields name back, so the
        # rewriter has to guess — when out_fields was non-default
        # it's the most likely cause.
        msg = "Cannot perform query. Invalid query parameters."
        details = ["Unable to perform query. Please check your parameters."]
        out = AnchorageGISPlugin._rewrite_arcgis_error(
            msg, details, resource_id="abc123",
            has_out_fields=True, has_where=False,
        )
        assert "out_fields" in out
        assert "get_layer_schema" in out
        assert "abc123" in out

    def test_unknown_error_passes_through_with_original_text(self):
        # Errors we don't recognize should still surface the upstream
        # message — better a verbose error than a misleading one.
        msg = "Some upstream failure"
        details = ["Database connection lost"]
        out = AnchorageGISPlugin._rewrite_arcgis_error(
            msg, details, resource_id="abc123",
        )
        assert "Some upstream failure" in out
        assert "Database connection lost" in out

    def test_no_data_hint_skips_trivial_where(self):
        # An empty/1=1 WHERE returning 0 records means the layer is
        # empty, not that the query was wrong — no LIKE hint helps.
        assert AnchorageGISPlugin._no_data_hint("") == ""
        assert AnchorageGISPlugin._no_data_hint("1=1") == ""
        assert AnchorageGISPlugin._no_data_hint("  1=1  ") == ""

    def test_no_data_hint_emits_for_non_trivial_where(self):
        out = AnchorageGISPlugin._no_data_hint("Name='Town Square'")
        assert "LIKE" in out
        assert "%" in out
        assert "CASE-SENSITIVE" in out
        assert "1=1" in out  # also tells the model how to confirm

    def test_not_queryable_message_names_recovery_path(self):
        out = AnchorageGISPlugin._not_queryable_message(
            "abc123", "Web Map"
        )
        assert "abc123" in out
        assert "Web Map" in out
        assert "find_gis_content" in out
        assert "QUERYABLE" in out


# ── Config schema ──────────────────────────────────────────────────────


class TestGetDistinctValues:
    """The R2M-vs-R-2M discovery problem. The model needs to confirm
    the exact format an identifier/code/category is stored in before
    constructing a WHERE clause that won't silently return zero rows."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_returns_distinct_values_with_next_step_hint(
        self, plugin
    ):
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://example.com/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value={"fields": [{"name": "ZONE_CODE"}]},
        ):
            mock_resp = Mock()
            mock_resp.status_code = 200
            mock_resp.raise_for_status = Mock()
            mock_resp.json.return_value = {
                "features": [
                    {"attributes": {"ZONE_CODE": "R-2M"}},
                    {"attributes": {"ZONE_CODE": "R-3"}},
                    {"attributes": {"ZONE_CODE": "B-1"}},
                ]
            }
            plugin.client = AsyncMock()
            plugin.client.get = AsyncMock(return_value=mock_resp)

            text = await plugin._get_distinct_values({
                "item_id": "a" * 32,
                "field": "ZONE_CODE",
            })

        # Stored values shown verbatim with their actual format.
        assert "`R-2M`" in text
        assert "`R-3`" in text
        assert "`B-1`" in text
        # Next-step trail tells the model how to use them.
        assert "CASE-SENSITIVE" in text
        assert "query_data" in text

    @pytest.mark.asyncio
    async def test_like_substring_filters_results(self, plugin):
        # Verify the LIKE filter is built correctly and passed through.
        captured_params = {}

        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://example.com/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value={"fields": [{"name": "ZONE_CODE"}]},
        ):
            async def fake_get(url, params=None):
                captured_params.update(params or {})
                resp = Mock()
                resp.status_code = 200
                resp.raise_for_status = Mock()
                resp.json.return_value = {
                    "features": [{"attributes": {"ZONE_CODE": "R-2M"}}]
                }
                return resp

            plugin.client = Mock()
            plugin.client.get = fake_get
            await plugin._get_distinct_values({
                "item_id": "a" * 32,
                "field": "ZONE_CODE",
                "like": "2M",
            })

        # The where clause must contain the LIKE pattern.
        assert "LIKE" in captured_params["where"]
        assert "%2M%" in captured_params["where"]
        # ArcGIS distinct-values flag must be on.
        assert captured_params["returnDistinctValues"] == "true"

    @pytest.mark.asyncio
    async def test_unknown_field_names_recovery_call(self, plugin):
        # If the model passes a bad field, the error should name
        # get_layer_schema as the recovery — not a stack trace.
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            return_value="https://example.com/FeatureServer/0",
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value={"fields": [{"name": "ZONE_CODE"}]},
        ):
            with pytest.raises(ValueError, match="get_layer_schema"):
                await plugin._get_distinct_values({
                    "item_id": "a" * 32,
                    "field": "made_up_field",
                })


class TestNormalizeParcelVariants:
    """Pure normaliser for MOA parcel IDs. Generates the four
    canonical formats (8-digit compact + hyphenated, 11-digit
    compact + hyphenated) from any common input form."""

    def test_hyphenated_8digit_input(self):
        # Input "001-213-29" — MOA-canonical short hyphenated form.
        out = AnchorageGISPlugin._normalize_parcel_variants("001-213-29")
        assert "00121329" in out
        assert "001-213-29" in out
        assert "00121329000" in out
        assert "001-213-29-000" in out

    def test_compact_8digit_input_pads_with_default_sub(self):
        # Compact 8-digit input — sub-parcel defaults to "000".
        out = AnchorageGISPlugin._normalize_parcel_variants("00121329")
        assert "00121329" in out
        assert "001-213-29" in out
        assert "00121329000" in out
        assert "001-213-29-000" in out

    def test_compact_11digit_input_preserves_sub(self):
        # Real sub-parcel "001" should round-trip in the variants.
        out = AnchorageGISPlugin._normalize_parcel_variants("00121329001")
        assert "00121329001" in out
        assert "001-213-29-001" in out
        # Both 8-digit forms should also be present (so model can find
        # the parent parcel across layers that drop the sub).
        assert "00121329" in out
        assert "001-213-29" in out

    def test_input_with_leading_zero_dropped(self):
        # User typed "1-213-29" — 6 digits, missing leading zeros.
        # Must still recover the canonical "001-213-29" form.
        out = AnchorageGISPlugin._normalize_parcel_variants("1-213-29")
        assert "001-213-29" in out
        assert "00121329" in out

    def test_input_with_prefix_text(self):
        # Real-world: "Parcel 003-184-87". Text prefix should not
        # break extraction.
        out = AnchorageGISPlugin._normalize_parcel_variants(
            "Parcel 003-184-87"
        )
        assert "00318487" in out
        assert "003-184-87" in out
        assert "00318487000" in out

    def test_too_short_returns_empty(self):
        # < 5 digits is too ambiguous to normalise; refuse rather than
        # generate misleading variants.
        assert AnchorageGISPlugin._normalize_parcel_variants("12") == []
        assert AnchorageGISPlugin._normalize_parcel_variants("") == []
        assert AnchorageGISPlugin._normalize_parcel_variants(None) == []

    def test_no_digits_returns_empty(self):
        assert AnchorageGISPlugin._normalize_parcel_variants(
            "no digits here"
        ) == []


class TestFindFeaturesSpanningClassifications:
    """The split-zoned-parcel pattern, generalised. A source feature
    (parcel, address, road) qualifies if its footprint touches >=
    min_distinct distinct values of a classification field on a
    polygon layer."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_finds_features_touching_multiple_classifications(
        self, plugin
    ):
        # 3 zone polygons (R-1, R-2M, B-1) and 3 parcels:
        #   parcel 100 — touches R-1 and R-2M (qualifies, 2 distinct)
        #   parcel 200 — touches R-1, R-2M, B-1 (qualifies, 3 distinct)
        #   parcel 300 — touches R-1 only (does not qualify)
        # Each call to source/query with one zone polygon returns the
        # parcel OBJECTIDs that touch that polygon.
        cls_polys = [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [0, 0], [1, 0], [1, 1], [0, 1], [0, 0],
                    ]],
                },
                "properties": {"ZONE_CODE": "R-1"},
            },
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [1, 0], [2, 0], [2, 1], [1, 1], [1, 0],
                    ]],
                },
                "properties": {"ZONE_CODE": "R-2M"},
            },
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [2, 0], [3, 0], [3, 1], [2, 1], [2, 0],
                    ]],
                },
                "properties": {"ZONE_CODE": "B-1"},
            },
        ]
        # Per-zone spatial-query results (in order).
        per_zone_oids = [
            {"objectIds": [100, 200, 300]},  # R-1
            {"objectIds": [100, 200]},        # R-2M
            {"objectIds": [200]},             # B-1
        ]

        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            side_effect=[
                "https://example.com/source/0",
                "https://example.com/cls/0",
            ],
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value={
                "geometryType": "esriGeometryPolygon",
                "fields": [{"name": "ZONE_CODE"}],
            },
        ), patch.object(
            plugin,
            "_get_record_count",
            new_callable=AsyncMock,
            return_value=3,
        ), patch.object(
            plugin,
            "_paged_geojson_fetch",
            new_callable=AsyncMock,
            return_value=cls_polys,
        ):
            spatial_calls = iter(per_zone_oids)
            attrs_resp = {
                "features": [
                    {"attributes": {"OBJECTID": 100, "Name": "Lot A"}},
                    {"attributes": {"OBJECTID": 200, "Name": "Lot B"}},
                ]
            }

            async def fake_post(url, data=None):
                # Attribute fetch is now POST too — distinguish by
                # which params are present.
                resp = Mock()
                resp.status_code = 200
                resp.raise_for_status = Mock()
                if (data or {}).get("objectIds"):
                    resp.json.return_value = attrs_resp
                else:
                    resp.json.return_value = next(spatial_calls)
                return resp

            plugin.client = Mock()
            plugin.client.post = fake_post

            text = await plugin._find_features_spanning_classifications({
                "source_item_id": "a" * 32,
                "classification_item_id": "b" * 32,
                "classification_field": "ZONE_CODE",
            })

        # Both qualifying parcels listed with their actual zone codes.
        assert "OBJECTID 100" in text
        assert "OBJECTID 200" in text
        assert "OBJECTID 300" not in text  # only touches 1 zone
        # Lot 200 touches all three zones.
        assert "`B-1`" in text
        assert "`R-1`" in text
        assert "`R-2M`" in text
        # Histogram shows distribution across ALL inspected features.
        assert "touches 1 distinct value(s):" in text
        assert "touches 2 distinct value(s):" in text
        assert "touches 3 distinct value(s):" in text
        # Qualifying count appears in the header.
        assert "**Qualifying:** 2" in text

    @pytest.mark.asyncio
    async def test_refuses_when_source_exceeds_cap(self, plugin):
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            side_effect=[
                "https://example.com/source/0",
                "https://example.com/cls/0",
            ],
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value={
                "geometryType": "esriGeometryPolygon",
                "fields": [{"name": "ZONE_CODE"}],
            },
        ), patch.object(
            plugin,
            "_get_record_count",
            new_callable=AsyncMock,
            return_value=99999,
        ):
            with pytest.raises(
                ValueError, match="exceeding the cap"
            ):
                await plugin._find_features_spanning_classifications({
                    "source_item_id": "a" * 32,
                    "classification_item_id": "b" * 32,
                    "classification_field": "ZONE_CODE",
                })

    @pytest.mark.asyncio
    async def test_classification_field_validated_against_schema(
        self, plugin
    ):
        # Bad classification_field should fail with a message that
        # names get_layer_schema as the recovery — same UX pattern as
        # the rest of the plugin.
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            side_effect=[
                "https://example.com/source/0",
                "https://example.com/cls/0",
            ],
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value={
                "geometryType": "esriGeometryPolygon",
                "fields": [{"name": "ZONE_CODE"}],
            },
        ):
            with pytest.raises(ValueError, match="get_layer_schema"):
                await plugin._find_features_spanning_classifications({
                    "source_item_id": "a" * 32,
                    "classification_item_id": "b" * 32,
                    "classification_field": "made_up_field",
                })

    @pytest.mark.asyncio
    async def test_attribute_fetch_failure_warns_loudly(self, plugin):
        # Regression for the GPT-4o-reports-OBJECTID-as-parcel-number
        # bug: when the bulk attribute fetch returns no records (rate
        # limit, transient upstream error), the response MUST warn
        # the model that OBJECTIDs are NOT user-facing identifiers
        # and must not be reported as parcel numbers / addresses.
        # Previously the silent fallback to "_note: attributes
        # unavailable" let the model render "OBJECTID 1747" as the
        # answer to "list parcel numbers".
        cls_polys = [
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [0, 0], [1, 0], [1, 1], [0, 1], [0, 0],
                    ]],
                },
                "properties": {"ZONE_CODE": "R-1"},
            },
            {
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [1, 0], [2, 0], [2, 1], [1, 1], [1, 0],
                    ]],
                },
                "properties": {"ZONE_CODE": "R-2M"},
            },
        ]

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            side_effect=[
                "https://example.com/source/0",
                "https://example.com/cls/0",
            ],
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={
                "geometryType": "esriGeometryPolygon",
                "fields": [{"name": "ZONE_CODE"}],
            },
        ), patch.object(
            plugin, "_get_record_count", new_callable=AsyncMock,
            return_value=2,
        ), patch.object(
            plugin, "_paged_geojson_fetch", new_callable=AsyncMock,
            return_value=cls_polys,
        ):
            async def fake_post(url, data=None):
                resp = Mock()
                resp.status_code = 200
                resp.raise_for_status = Mock()
                if (data or {}).get("objectIds"):
                    # Simulate the rate-limit failure: no error key,
                    # just an empty features list (the silent shape
                    # we used to render as "_note: attributes
                    # unavailable").
                    resp.json.return_value = {"features": []}
                else:
                    # Spatial-query side: both zones contain parcel 100.
                    resp.json.return_value = {"objectIds": [100]}
                return resp

            plugin.client = Mock()
            plugin.client.post = fake_post

            text = await plugin._find_features_spanning_classifications(
                {
                    "source_item_id": "a" * 32,
                    "classification_item_id": "b" * 32,
                    "classification_field": "ZONE_CODE",
                }
            )

        # Parcel 100 touches both zones → qualifies. Attribute fetch
        # returned empty → the warning must fire.
        assert "WARNING" in text
        assert "INTERNAL LAYER ROW IDs" in text
        assert "DO NOT report them" in text
        # The model is told how to recover.
        assert "query_data" in text
        assert "OBJECTID=" in text
        # The qualifying OID is still surfaced (with the value set)
        # so the model has something to work with.
        assert "100" in text
        # The dangerous old marker must not be present anywhere.
        assert "_note: attributes unavailable" not in text

    @pytest.mark.asyncio
    async def test_classification_must_be_polygon(self, plugin):
        with patch.object(
            plugin,
            "_resolve_layer_url",
            new_callable=AsyncMock,
            side_effect=[
                "https://example.com/source/0",
                "https://example.com/cls/0",
            ],
        ), patch.object(
            plugin,
            "_fetch_layer_meta",
            new_callable=AsyncMock,
            return_value={
                "geometryType": "esriGeometryPoint",
                "fields": [{"name": "ZONE_CODE"}],
            },
        ):
            with pytest.raises(
                ValueError,
                match="classification_item_id must point at a polygon",
            ):
                await plugin._find_features_spanning_classifications({
                    "source_item_id": "a" * 32,
                    "classification_item_id": "b" * 32,
                    "classification_field": "ZONE_CODE",
                })


class TestFindParcel:
    """Integration of the parcel normaliser with a real WHERE IN
    lookup. Validates that the variants are sent to the upstream
    correctly and that the response surfaces the canonical stored
    form for follow-up queries."""

    @pytest.fixture
    def plugin(self, anchorage_config):
        p = AnchorageGISPlugin(anchorage_config)
        p.plugin_config = AnchorageGISPluginConfig(**anchorage_config)
        return p

    @pytest.mark.asyncio
    async def test_in_clause_carries_all_variants(self, plugin):
        # Verify the WHERE IN actually contains every generated form,
        # so a layer storing any of them would match.
        captured = {}

        async def fake_get(url, params=None):
            captured.update(params or {})
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            resp.json.return_value = {
                "features": [
                    {"attributes": {"Parcel_Num": "00121329000"}}
                ]
            }
            return resp

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            plugin.client = Mock()
            plugin.client.get = fake_get

            await plugin._find_parcel({
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "001-213-29",
            })

        where = captured["where"]
        # All four canonical forms must be in the IN clause.
        assert "'00121329'" in where
        assert "'001-213-29'" in where
        assert "'00121329000'" in where
        assert "'001-213-29-000'" in where
        assert "Parcel_Num IN" in where

    @pytest.mark.asyncio
    async def test_match_surfaces_canonical_stored_form(self, plugin):
        # The layer happens to store 11-digit compact. Response should
        # tell the model what stored format matched, so follow-up
        # queries on this layer use the right form verbatim.
        async def fake_get(url, params=None):
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            resp.json.return_value = {
                "features": [
                    {
                        "attributes": {
                            "Parcel_Num": "00318487000",
                            "Source": "P-464",
                        }
                    }
                ]
            }
            return resp

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            plugin.client = Mock()
            plugin.client.get = fake_get

            text = await plugin._find_parcel({
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "003-184-87",
            })

        assert "00318487000" in text
        assert "Canonical form for this layer" in text
        assert "P-464" in text  # other fields surfaced too

    @pytest.mark.asyncio
    async def test_no_match_falls_back_to_like(self, plugin):
        # First call (IN) returns no features; second call (LIKE)
        # returns 2 candidates. The response should surface the
        # candidates so the model has something to act on.
        call_count = {"n": 0}

        async def fake_get(url, params=None):
            call_count["n"] += 1
            resp = Mock()
            resp.status_code = 200
            resp.raise_for_status = Mock()
            if call_count["n"] == 1:
                # Exact-match IN returns nothing.
                resp.json.return_value = {"features": []}
            else:
                # LIKE fallback finds candidates.
                resp.json.return_value = {
                    "features": [
                        {"attributes": {"Parcel_Num": "00121329111"}},
                        {"attributes": {"Parcel_Num": "00121329222"}},
                    ]
                }
            return resp

        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            plugin.client = Mock()
            plugin.client.get = fake_get

            text = await plugin._find_parcel({
                "item_id": "a" * 32,
                "parcel_field": "Parcel_Num",
                "parcel_id": "001-213-29",
            })

        assert "no exact match" in text
        assert "LIKE fallback" in text
        assert "00121329111" in text
        assert "00121329222" in text

    @pytest.mark.asyncio
    async def test_unknown_parcel_field_names_recovery(self, plugin):
        # Bad field name should give the model a clear path to recover
        # — same UX pattern as the rest of the plugin's errors.
        with patch.object(
            plugin, "_resolve_layer_url", new_callable=AsyncMock,
            return_value="https://example.com/Layer/0",
        ), patch.object(
            plugin, "_fetch_layer_meta", new_callable=AsyncMock,
            return_value={"fields": [{"name": "Parcel_Num"}]},
        ):
            with pytest.raises(ValueError, match="get_layer_schema"):
                await plugin._find_parcel({
                    "item_id": "a" * 32,
                    "parcel_field": "made_up_field",
                    "parcel_id": "001-213-29",
                })


class TestConfigSchema:
    def test_config_schema_valid(self):
        config = AnchorageGISPluginConfig(
            portal_base_url="https://muniorg.maps.arcgis.com/sharing/rest",
            gallery_group_id="c34ed10758ec4f4eb8aa6826ee5be3ff",
            org_id="Ce3DhLRthdwbHlfF",
            city_name="Municipality of Anchorage",
            gallery_url=(
                "https://muniorg.maps.arcgis.com/apps/instant/filtergallery/"
                "index.html?appid=4dac7569f1cc4beb9f22ce168c899a30"
            ),
            timeout=30,
        )
        assert config.city_name == "Municipality of Anchorage"
        assert config.org_id == "Ce3DhLRthdwbHlfF"
        assert config.timeout == 30

    def test_config_schema_rejects_extra_fields(self):
        with pytest.raises(ValidationError):
            AnchorageGISPluginConfig(
                portal_base_url="https://muniorg.maps.arcgis.com/sharing/rest",
                gallery_group_id="test",
                org_id="test",
                city_name="Test",
                gallery_url="https://example.com/gallery",
                unknown_field="oops",
            )

    def test_config_schema_strips_trailing_slash(self):
        config = AnchorageGISPluginConfig(
            portal_base_url="https://muniorg.maps.arcgis.com/sharing/rest/",
            gallery_group_id="test",
            org_id="test",
            city_name="Test",
            gallery_url="https://example.com/gallery",
        )
        assert config.portal_base_url == (
            "https://muniorg.maps.arcgis.com/sharing/rest"
        )

    def test_config_schema_rejects_invalid_url(self):
        with pytest.raises(ValidationError):
            AnchorageGISPluginConfig(
                portal_base_url="not-a-url",
                gallery_group_id="test",
                org_id="test",
                city_name="Test",
                gallery_url="https://example.com/gallery",
            )

    def test_config_schema_rejects_empty_portal_url(self):
        with pytest.raises(ValidationError):
            AnchorageGISPluginConfig(
                portal_base_url="",
                gallery_group_id="test",
                org_id="test",
                city_name="Test",
                gallery_url="https://example.com/gallery",
            )

    def test_config_schema_rejects_empty_gallery_url(self):
        with pytest.raises(ValidationError):
            AnchorageGISPluginConfig(
                portal_base_url="https://muniorg.maps.arcgis.com/sharing/rest",
                gallery_group_id="test",
                org_id="test",
                city_name="Test",
                gallery_url="",
            )
