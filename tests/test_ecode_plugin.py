"""Tests for the eCode360 municipal code plugin.

Verify config validation, credential resolution, initialization, tool
definitions, tool dispatch against mocked HTTP responses, formatting, and the
toc→structure fallback. Designed to fail if functionality breaks.
"""

import os

import httpx
import pytest
from pydantic import ValidationError
from unittest.mock import AsyncMock, Mock, patch

from core.interfaces import PluginType
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import EcodePlugin


@pytest.fixture
def ecode_config():
    return {
        "enabled": True,
        "base_url": "https://api.ecode360.com",
        "customer_id": "AN6998",
        "city_name": "Municipality of Anchorage",
        "timeout": 60,
    }


def _mock_response(json_body=None, status_code=200):
    resp = Mock()
    resp.status_code = status_code
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=json_body or {})
    resp.text = ""
    return resp


def _plugin_with_client(ecode_config, get_side_effect=None, get_return=None):
    """Build an initialized-enough plugin with a mocked httpx client."""
    plugin = EcodePlugin(ecode_config)
    plugin.plugin_config = EcodePluginConfig(**ecode_config)
    client = AsyncMock()
    if get_side_effect is not None:
        client.get = AsyncMock(side_effect=get_side_effect)
    else:
        client.get = AsyncMock(return_value=get_return)
    plugin.client = client
    return plugin


# ── Plugin attributes ──────────────────────────────────────────────────


class TestPluginAttributes:
    def test_attributes(self, ecode_config):
        plugin = EcodePlugin(ecode_config)
        assert plugin.plugin_name == "ecode"
        assert plugin.plugin_type == PluginType.CUSTOM_API


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_valid(self, ecode_config):
        cfg = EcodePluginConfig(**ecode_config)
        assert cfg.customer_id == "AN6998"
        assert cfg.base_url == "https://api.ecode360.com"
        assert cfg.api_key_env == "ECODE_API_KEY"

    def test_strips_trailing_slash(self, ecode_config):
        cfg = EcodePluginConfig(
            **{**ecode_config, "base_url": "https://api.ecode360.com/"}
        )
        assert cfg.base_url == "https://api.ecode360.com"

    def test_invalid_customer_id(self, ecode_config):
        with pytest.raises(ValidationError):
            EcodePluginConfig(**{**ecode_config, "customer_id": "AN 6998!"})

    def test_invalid_url(self, ecode_config):
        with pytest.raises(ValidationError):
            EcodePluginConfig(**{**ecode_config, "base_url": "ftp://x"})

    def test_extra_forbidden(self, ecode_config):
        with pytest.raises(ValidationError):
            EcodePluginConfig(**{**ecode_config, "bogus": 1})


# ── Credential resolution ──────────────────────────────────────────────


class TestCredentials:
    def test_from_env(self, ecode_config, monkeypatch):
        monkeypatch.setenv("ECODE_API_KEY", "k1")
        monkeypatch.setenv("ECODE_API_SECRET", "s1")
        cfg = EcodePluginConfig(**ecode_config)
        assert EcodePlugin._resolve_credentials(cfg) == ("k1", "s1")

    def test_from_secrets_file(self, ecode_config, tmp_path, monkeypatch):
        monkeypatch.delenv("ECODE_API_KEY", raising=False)
        monkeypatch.delenv("ECODE_API_SECRET", raising=False)
        f = tmp_path / ".ecode.env"
        f.write_text('ECODE_API_KEY="fk"\n# comment\nECODE_API_SECRET=fs\n')
        cfg = EcodePluginConfig(**{**ecode_config, "secrets_file": str(f)})
        assert EcodePlugin._resolve_credentials(cfg) == ("fk", "fs")
        # cleanup env the loader set
        os.environ.pop("ECODE_API_KEY", None)
        os.environ.pop("ECODE_API_SECRET", None)

    def test_missing(self, ecode_config, monkeypatch):
        monkeypatch.delenv("ECODE_API_KEY", raising=False)
        monkeypatch.delenv("ECODE_API_SECRET", raising=False)
        cfg = EcodePluginConfig(**ecode_config)
        assert EcodePlugin._resolve_credentials(cfg) == ("", "")


# ── Initialization ─────────────────────────────────────────────────────


class TestInitialization:
    @pytest.mark.asyncio
    async def test_success(self, ecode_config, monkeypatch):
        monkeypatch.setenv("ECODE_API_KEY", "k")
        monkeypatch.setenv("ECODE_API_SECRET", "s")
        plugin = EcodePlugin(ecode_config)
        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.get = AsyncMock(return_value=_mock_response({"error": False}))
            mock_cls.return_value = client
            assert await plugin.initialize() is True
            assert plugin._initialized is True

    @pytest.mark.asyncio
    async def test_missing_credentials_fails(self, ecode_config, monkeypatch):
        monkeypatch.delenv("ECODE_API_KEY", raising=False)
        monkeypatch.delenv("ECODE_API_SECRET", raising=False)
        plugin = EcodePlugin(ecode_config)
        assert await plugin.initialize() is False
        assert plugin._initialized is False

    @pytest.mark.asyncio
    async def test_bad_key_401_fails(self, ecode_config, monkeypatch):
        monkeypatch.setenv("ECODE_API_KEY", "k")
        monkeypatch.setenv("ECODE_API_SECRET", "s")
        plugin = EcodePlugin(ecode_config)
        err_resp = Mock(status_code=401, text="Unauthorized")
        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.get = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "401", request=Mock(), response=err_resp
                )
            )
            mock_cls.return_value = client
            assert await plugin.initialize() is False


# ── Tool definitions ───────────────────────────────────────────────────


class TestGetTools:
    def test_tool_set(self, ecode_config):
        plugin = EcodePlugin(ecode_config)
        plugin.plugin_config = EcodePluginConfig(**ecode_config)
        names = {t.name for t in plugin.get_tools()}
        assert names == {
            "search_code",
            "get_table_of_contents",
            "browse_structure",
            "get_section",
            "get_customer_info",
        }

    def test_all_read_only(self, ecode_config):
        plugin = EcodePlugin(ecode_config)
        plugin.plugin_config = EcodePluginConfig(**ecode_config)
        for t in plugin.get_tools():
            assert t.annotations and t.annotations.get("readOnlyHint") is True


# ── Tool dispatch ──────────────────────────────────────────────────────


class TestExecuteTool:
    @pytest.mark.asyncio
    async def test_unknown(self, ecode_config):
        plugin = _plugin_with_client(ecode_config, get_return=_mock_response({}))
        result = await plugin.execute_tool("nope", {})
        assert result.success is False
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_search_requires_query(self, ecode_config):
        plugin = _plugin_with_client(ecode_config, get_return=_mock_response({}))
        result = await plugin.execute_tool("search_code", {})
        assert result.success is False
        assert "query is required" in result.error_message

    @pytest.mark.asyncio
    async def test_search_code(self, ecode_config):
        # Shape mirrors the live API: guid in `id`, snippet in `text`, `page`
        # comes back as a string, and title/snippet carry <em> highlight markup.
        body = {
            "page": "0",
            "maxPages": 3,
            "totalResultCount": 53,
            "query": "fence",
            "results": [
                {
                    "id": "8682677",
                    "title": '<em class="highlight">Fence</em>s and walls.',
                    "number": "§ 120-167",
                    "type": "section",
                    "url": "https://live.ecode360.com/8682677?highlight=fence#8682677",
                    "pdf": False,
                    "text": "the <em>fence</em> requirements",
                }
            ],
        }
        plugin = _plugin_with_client(ecode_config, get_return=_mock_response(body))
        result = await plugin.execute_tool("search_code", {"query": "fence"})
        assert result.success is True
        text = result.content[0]["text"]
        assert "guid: 8682677" in text
        assert "https://ecode360.com/8682677" in text  # clean public link
        assert "53 result" in text
        assert "page=1" in text  # paging hint
        assert "<em>" not in text  # html stripped from title and snippet

    @pytest.mark.asyncio
    async def test_get_section(self, ecode_config):
        body = {
            "id": "8682677",
            "number": "§ 120-167",
            "title": "Fences and walls.",
            "type": "section",
            "label": "Section",
            "content": "Fences and walls.\nRequirements apply.",
            "images": [],
            "attachments": {},
            "children": [
                {
                    "number": "A.",
                    "title": "",
                    "content": "No fence over 6 feet.",
                    "children": [],
                }
            ],
        }
        plugin = _plugin_with_client(ecode_config, get_return=_mock_response(body))
        result = await plugin.execute_tool("get_section", {"guid": "8682677"})
        assert result.success is True
        text = result.content[0]["text"]
        assert "Requirements apply." in text
        assert "No fence over 6 feet." in text
        assert "ecode360.com/8682677" in text

    @pytest.mark.asyncio
    async def test_get_section_requires_guid(self, ecode_config):
        plugin = _plugin_with_client(ecode_config, get_return=_mock_response({}))
        result = await plugin.execute_tool("get_section", {})
        assert result.success is False
        assert "guid is required" in result.error_message

    @pytest.mark.asyncio
    async def test_browse_structure(self, ecode_config):
        # Live structure responses wrap the node in an {error, results} envelope.
        body = {
            "error": False,
            "results": {
                "guid": "8672824",
                "title": "Taxes",
                "number": "Ch 2",
                "type": "chapter",
                "label": "Chapter",
                "parent": "8672823",
                "children": [{"guid": "1", "number": "§ 2-1", "title": "Levy"}],
            },
        }
        plugin = _plugin_with_client(ecode_config, get_return=_mock_response(body))
        result = await plugin.execute_tool("browse_structure", {"guid": "8672824"})
        assert result.success is True
        text = result.content[0]["text"]
        assert "Taxes" in text and "Levy" in text and "guid: 1" in text

    @pytest.mark.asyncio
    async def test_get_customer_info(self, ecode_config):
        body = {
            "error": False,
            "results": {
                "id": "AN6998",
                "name": "Municipality of Anchorage, AK",
                "shortName": "Anchorage",
                "state": "AK",
                "govtype": "Municipality",
                "updatedDate": "2024-01-01",
                "url": "https://ecode360.com/AN6998",
            },
        }
        plugin = _plugin_with_client(ecode_config, get_return=_mock_response(body))
        result = await plugin.execute_tool("get_customer_info", {})
        assert result.success is True
        assert "Anchorage" in result.content[0]["text"]
        assert "AN6998" in result.content[0]["text"]

    @pytest.mark.asyncio
    async def test_toc_falls_back_to_structure_on_404(self, ecode_config):
        err_resp = Mock(status_code=404, text="Not Found")
        err_resp.json = Mock(return_value={"message": "Not Found"})
        structure_body = {
            "guid": "ROOT",
            "title": "Anchorage Municipal Code",
            "children": [{"guid": "10", "number": "Title 1", "title": "General"}],
        }
        side_effect = [
            httpx.HTTPStatusError("404", request=Mock(), response=err_resp),
            _mock_response(structure_body),
        ]
        plugin = _plugin_with_client(ecode_config, get_side_effect=side_effect)
        result = await plugin.execute_tool("get_table_of_contents", {})
        assert result.success is True
        text = result.content[0]["text"]
        assert "General" in text
        assert "fallback" in text.lower() or "structure" in text.lower()


# ── Health check ───────────────────────────────────────────────────────


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self, ecode_config):
        plugin = _plugin_with_client(
            ecode_config, get_return=_mock_response({}, status_code=200)
        )
        assert await plugin.health_check() is True

    @pytest.mark.asyncio
    async def test_unhealthy(self, ecode_config):
        plugin = _plugin_with_client(
            ecode_config, get_side_effect=httpx.ConnectError("down")
        )
        assert await plugin.health_check() is False


# ── Small utilities ────────────────────────────────────────────────────


class TestUtilities:
    def test_guid_from_url(self):
        assert EcodePlugin._guid_from_url("https://ecode360.com/8682677") == "8682677"
        assert EcodePlugin._guid_from_url("https://ecode360.com/8682677/") == "8682677"
        assert EcodePlugin._guid_from_url("") == ""

    def test_strip_html(self):
        assert EcodePlugin._strip_html("a <em>b</em> c") == "a b c"
