"""eCode360 (General Code) municipal code plugin for OpenContext.

Provides conversational, read-only access to a single municipality's published
municipal code hosted on the eCode360 platform (the "EcodeGateway" API at
https://api.ecode360.com). The municipality is fixed per deployment by
``customer_id`` in config (e.g. ``AN6998`` for the Municipality of Anchorage),
so the tools never ask the model for a customer id.

API reference (v2.7): two API-key headers (``api-key`` / ``api-secret``) on
every request; all endpoints are GET under ``/v1`` and return JSON.
"""

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
from plugins.ecode.config_schema import EcodePluginConfig

logger = logging.getLogger(__name__)

# Public, human-facing URL for any node — its guid is the path segment.
PUBLIC_URL = "https://ecode360.com/{guid}"

# Cap rendered section text so a deeply nested chapter cannot blow up a response.
MAX_CONTENT_CHARS = 14000


class EcodePlugin(MCPPlugin):
    """Plugin for querying a municipality's eCode360-hosted municipal code."""

    plugin_name = "ecode"
    plugin_type = PluginType.CUSTOM_API
    plugin_version = "1.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[EcodePluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        try:
            self.plugin_config = EcodePluginConfig(**self.config)

            api_key, api_secret = self._resolve_credentials(self.plugin_config)
            if not api_key or not api_secret:
                logger.error(
                    "eCode plugin missing credentials: set %s and %s in the "
                    "environment (Lambda env vars) or via secrets_file for local dev.",
                    self.plugin_config.api_key_env,
                    self.plugin_config.api_secret_env,
                )
                return False

            self.client = httpx.AsyncClient(
                base_url=f"{self.plugin_config.base_url}/v1",
                headers={
                    "api-key": api_key,
                    "api-secret": api_secret,
                    "Accept": "application/json",
                },
                timeout=self.plugin_config.timeout,
            )

            # Verify connectivity + credentials against the (cheap) customer
            # metadata endpoint. 401/403 here means a bad key or no access.
            resp = await self.client.get(self._cust_path(""))
            resp.raise_for_status()

            self._initialized = True
            logger.info(
                "eCode plugin initialized for %s (customer %s)",
                self.plugin_config.city_name,
                self.plugin_config.customer_id,
            )
            return True

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            hint = (
                " (check ECODE_API_KEY / ECODE_API_SECRET)"
                if status in (401, 403)
                else ""
            )
            logger.error(
                "eCode plugin init failed: HTTP %s%s: %s",
                status,
                hint,
                e.response.text[:500],
            )
            return False
        except Exception as e:
            logger.error("Failed to initialize eCode plugin: %s", e, exc_info=True)
            return False

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("eCode plugin shut down")

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(self._cust_path(""))
            return resp.status_code == 200
        except Exception as e:
            logger.error("eCode health check failed: %s", e)
            return False

    # ── Tool definitions ────────────────────────────────────────────────

    def get_tools(self) -> List[ToolDefinition]:
        city = (
            self.plugin_config.city_name if self.plugin_config else "the municipality"
        )
        read_only = {"readOnlyHint": True, "openWorldHint": True}
        return [
            ToolDefinition(
                name="search_code",
                description=(
                    f"Full-text search of {city}'s municipal code. Returns up to 20 "
                    "matching sections per page, each with its title, reference "
                    "number, a highlighted snippet, and a guid. Use the guid with "
                    "get_section to read the full text. Supports eCode360 advanced "
                    'syntax, e.g. proximity: "city council"~3. Page through results '
                    "with the page argument (0-indexed)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search term or advanced-syntax query",
                        },
                        "page": {
                            "type": "integer",
                            "description": "0-indexed result page (20 results/page)",
                            "default": 0,
                            "minimum": 0,
                        },
                    },
                    "required": ["query"],
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_table_of_contents",
                description=(
                    f"Get the top-level table of contents of {city}'s municipal code "
                    "(titles/chapters down to the section level, without body text). "
                    "Each entry has a guid you can pass to browse_structure or "
                    "get_section. Start here to orient before drilling in."
                ),
                input_schema={"type": "object", "properties": {}},
                annotations=read_only,
            ),
            ToolDefinition(
                name="browse_structure",
                description=(
                    "Browse one node of the code hierarchy: returns the node's "
                    "number, title, type, and its immediate children (each with a "
                    "guid). Pass guid='ROOT' (the default) for the top of the tree, "
                    "then walk down using child guids. Use this to navigate the "
                    "outline; use get_section to read actual text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guid": {
                            "type": "string",
                            "description": "Node guid, or 'ROOT' for the top",
                            "default": "ROOT",
                        },
                    },
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_section",
                description=(
                    "Retrieve the full text of a code section (and its nested "
                    "subsections) by guid. The guid comes from search_code, "
                    "get_table_of_contents, or browse_structure. Also surfaces any "
                    "images/attachments and the public ecode360.com link."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guid": {
                            "type": "string",
                            "description": "Section/node guid (e.g. '8682677')",
                        },
                    },
                    "required": ["guid"],
                },
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_customer_info",
                description=(
                    f"Get metadata about {city}'s code library: official name, "
                    "state, government type, last-updated date, and public URL."
                ),
                input_schema={"type": "object", "properties": {}},
                annotations=read_only,
            ),
        ]

    # ── Tool dispatch ───────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "search_code":
                query = arguments.get("query")
                if not query:
                    return self._err("query is required")
                page = int(arguments.get("page", 0))
                data = await self._search(query, page)
                return self._ok(self._format_search(data))

            if tool_name == "get_table_of_contents":
                data = await self._get_toc()
                return self._ok(self._format_toc(data))

            if tool_name == "browse_structure":
                guid = arguments.get("guid") or "ROOT"
                data = await self._get_json(
                    self._cust_path(f"/structure/{quote(str(guid), safe='')}")
                )
                # structure responses wrap the node in an {error, results} envelope
                node = data.get("results", data) if isinstance(data, dict) else data
                return self._ok(self._format_structure(node))

            if tool_name == "get_section":
                guid = arguments.get("guid")
                if not guid:
                    return self._err("guid is required")
                data = await self._get_json(
                    self._cust_path(f"/code/content/{quote(str(guid), safe='')}")
                )
                return self._ok(self._format_content(data))

            if tool_name == "get_customer_info":
                data = await self._get_json(self._cust_path(""))
                return self._ok(self._format_customer(data))

            return self._err(f"Unknown tool: {tool_name}")

        except Exception as e:
            logger.error("Error executing tool %s: %s", tool_name, e, exc_info=True)
            return self._err(str(e) or "Tool execution failed")

    # ── HTTP helpers ────────────────────────────────────────────────────

    def _cust_path(self, suffix: str) -> str:
        """Path under /v1/customer/{customer_id}{suffix}."""
        cust = self.plugin_config.customer_id
        return f"/customer/{cust}{suffix}"

    async def _get_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        try:
            resp = await self.client.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"eCode API error (HTTP {e.response.status_code}) for {path}: "
                f"{self._extract_api_error(e.response)}"
            ) from e
        return resp.json()

    async def _search(self, query: str, page: int) -> Dict[str, Any]:
        return await self._get_json(
            self._cust_path("/search"), params={"query": query, "page": page}
        )

    async def _get_toc(self) -> Dict[str, Any]:
        """TOC endpoint, falling back to the structure ROOT if /code/toc is
        not available for this customer (the endpoint is absent from the older
        OpenAPI spec, so treat 404 as 'use structure instead')."""
        try:
            return await self._get_json(self._cust_path("/code/toc"))
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                logger.info("toc endpoint 404; falling back to structure ROOT")
                data = await self._get_json(self._cust_path("/structure/ROOT"))
                data["_fallback"] = "structure/ROOT (toc endpoint unavailable)"
                return data
            raise

    @staticmethod
    def _extract_api_error(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            msg = body.get("message") or body.get("exception")
            if msg:
                return str(msg)
        except Exception:
            pass
        return resp.text[:500]

    @staticmethod
    def _resolve_credentials(cfg: EcodePluginConfig) -> tuple:
        """Read api-key/api-secret from the environment, loading a local
        secrets file first if one is configured and the vars aren't set."""
        if cfg.secrets_file and (
            cfg.api_key_env not in os.environ or cfg.api_secret_env not in os.environ
        ):
            _load_env_file(cfg.secrets_file)
        return (
            (os.environ.get(cfg.api_key_env) or "").strip(),
            (os.environ.get(cfg.api_secret_env) or "").strip(),
        )

    # ── Result helpers ──────────────────────────────────────────────────

    @staticmethod
    def _ok(text: str) -> ToolResult:
        return ToolResult(content=[{"type": "text", "text": text}], success=True)

    @staticmethod
    def _err(message: str) -> ToolResult:
        return ToolResult(content=[], success=False, error_message=message)

    # ── Formatters ──────────────────────────────────────────────────────

    def _format_search(self, data: Dict[str, Any]) -> str:
        results = data.get("results", []) or []
        total = data.get("totalResultCount", len(results))
        page = self._as_int(data.get("page", 0))
        max_pages = self._as_int(data.get("maxPages", 1), default=1)
        if not results:
            return f"No results for query {data.get('query', '')!r}."

        lines = [
            f"{total} result(s) for {data.get('query', '')!r} "
            f"(page {page + 1} of {max_pages}, 20/page):",
            "",
        ]
        for i, r in enumerate(results, 1 + page * 20):
            # Search results key the node id as `id`; the snippet as `text`;
            # and both title and snippet may contain <em> highlight markup.
            guid = r.get("id") or self._guid_from_url(r.get("url", ""))
            snippet = self._strip_html(r.get("text") or r.get("snippet", ""))
            title = self._strip_html(r.get("title", "")) or "Untitled"
            heading = f"{r.get('number', '')} {title}".strip()
            lines.append(f"{i}. {heading}")
            lines.append(f"   guid: {guid or '(unknown)'}   type: {r.get('type', '')}")
            if r.get("pdf"):
                lines.append("   (PDF document)")
            if snippet:
                lines.append(f"   …{snippet}…")
            if guid:
                lines.append(f"   {PUBLIC_URL.format(guid=guid)}")
            lines.append("")
        if page + 1 < max_pages:
            lines.append(f"More results: call search_code with page={page + 1}.")
        return "\n".join(lines)

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _format_toc(self, data: Dict[str, Any]) -> str:
        header = []
        if data.get("_fallback"):
            header.append(f"(Note: {data['_fallback']})")
        title = data.get("title", "Table of Contents")
        header.append(f"Table of Contents — {title}")
        guid = data.get("guid") or data.get("id")
        if guid:
            header.append(f"root guid: {guid}")
        header.append("")
        body = self._render_children(data.get("children", []) or [], depth=0)
        return "\n".join(header) + ("\n".join(body) if body else "(no entries)")

    def _format_structure(self, data: Dict[str, Any]) -> str:
        guid = data.get("guid") or data.get("id", "")
        lines = [
            f"{data.get('label', '')} {data.get('number', '')} "
            f"{data.get('title', '')}".strip(),
            f"guid: {guid}   type: {data.get('type', '')}",
            f"parent: {data.get('parent') or '(none)'}",
            f"link: {PUBLIC_URL.format(guid=guid)}" if guid and guid != "ROOT" else "",
            "",
            "Children:",
        ]
        children = data.get("children", []) or []
        body = self._render_children(children, depth=0)
        lines.extend(body or ["  (none — this may be a leaf section; "
                              "use get_section to read its text)"])
        return "\n".join(line for line in lines if line != "")

    def _render_children(self, children: List[Any], depth: int) -> List[str]:
        """Render a list of child nodes (each a dict, or a bare guid string)."""
        out: List[str] = []
        indent = "  " * (depth + 1)
        for child in children:
            if isinstance(child, str):
                out.append(f"{indent}- guid {child}")
                continue
            guid = child.get("guid") or child.get("id", "")
            label = " ".join(
                p for p in [child.get("number", ""), child.get("title", "")] if p
            ).strip()
            out.append(f"{indent}- {label or '(untitled)'}  [guid: {guid}]")
        return out

    def _format_content(
        self, data: Dict[str, Any], _budget: Optional[List[int]] = None
    ) -> str:
        if _budget is None:
            _budget = [MAX_CONTENT_CHARS]
            guid = data.get("id") or data.get("guid", "")
            head = [
                " ".join(
                    p for p in [data.get("number", ""), data.get("title", "")] if p
                ).strip()
                or "(untitled section)",
                f"guid: {guid}   type: {data.get('type', '')}",
                f"link: {PUBLIC_URL.format(guid=guid)}" if guid else "",
            ]
            images = data.get("images") or []
            attachments = data.get("attachments") or {}
            if images:
                head.append(f"images: {len(images)} (relative links on ecode360.com)")
            if attachments:
                head.append(f"attachments: {', '.join(attachments.keys())}")
            head.append("")
            body = self._render_content_node(data, depth=0, budget=_budget)
            return "\n".join(h for h in head if h != "") + "\n" + body

        return self._render_content_node(data, depth=0, budget=_budget)

    def _render_content_node(
        self, node: Dict[str, Any], depth: int, budget: List[int]
    ) -> str:
        if budget[0] <= 0:
            return ""
        indent = "  " * depth
        parts: List[str] = []
        heading = " ".join(
            p for p in [node.get("number", ""), node.get("title", "")] if p
        ).strip()
        if depth > 0 and heading:
            parts.append(f"{indent}{heading}")
        text = (node.get("content") or "").strip()
        if text:
            if len(text) > budget[0]:
                text = text[: budget[0]] + " …[truncated]"
            budget[0] -= len(text)
            wrapped = "\n".join(f"{indent}{ln}" for ln in text.splitlines())
            parts.append(wrapped)
        for child in node.get("children", []) or []:
            if budget[0] <= 0:
                parts.append(f"{indent}…[output truncated; fetch child guids "
                             "individually with get_section]")
                break
            if isinstance(child, dict):
                parts.append(self._render_content_node(child, depth + 1, budget))
        return "\n".join(p for p in parts if p)

    def _format_customer(self, data: Dict[str, Any]) -> str:
        results = data.get("results", data) if isinstance(data, dict) else {}
        if not isinstance(results, dict):
            results = {}
        fields = [
            ("Name", results.get("name")),
            ("Short name", results.get("shortName")),
            ("Customer ID", results.get("id")),
            ("State", results.get("state")),
            ("Government type", results.get("govtype")),
            ("Population", results.get("population")),
            ("Last updated", results.get("updatedDate")),
            ("PDF only", results.get("pdfOnly")),
            ("URL", results.get("url")),
        ]
        return "\n".join(
            f"{label}: {value}" for label, value in fields if value not in (None, "")
        )

    # ── Small utilities ─────────────────────────────────────────────────

    @staticmethod
    def _guid_from_url(url: str) -> str:
        """Extract the trailing guid from an ecode360.com URL."""
        if not url:
            return ""
        return url.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _strip_html(text: str) -> str:
        import re

        return re.sub(r"<[^>]+>", "", text or "").strip()


def _load_env_file(path: str) -> None:
    """Load simple KEY=VALUE lines from a local env file into os.environ.

    Only sets vars that are not already present, so real environment / Lambda
    env vars always win. Silently no-ops if the file is missing.
    """
    try:
        # utf-8-sig tolerates a BOM (e.g. files saved by Windows Notepad).
        with open(path, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        logger.warning("secrets_file not found: %s", path)
    except Exception as e:
        logger.warning("failed reading secrets_file %s: %s", path, e)
