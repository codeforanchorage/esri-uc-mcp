"""Anchorage GIS plugin implementation for OpenContext.

This plugin provides access to the Municipality of Anchorage GIS Gallery
and spatial data via the ArcGIS Portal REST API. It exposes tools for
discovering maps, apps, and spatial datasets published by MOA GIS.
"""

import asyncio
import json
import logging
import math
import re
import time
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from core.interfaces import DataPlugin, PluginType, ToolDefinition, ToolResult
from plugins.anchorage_gis.config_schema import AnchorageGISPluginConfig
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)

logger = logging.getLogger(__name__)


class AnchorageGISPlugin(DataPlugin):
    """Plugin for accessing Municipality of Anchorage GIS data.

    Uses the ArcGIS Portal REST API to search the curated public gallery
    and the organization's spatial layers, retrieve item details, inspect
    Feature Service schemas, and query Feature Service records.
    """

    plugin_name = "anchorage_gis"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    GALLERY_APP_TYPES = [
        "Web Experience",
        "Web Mapping Application",
        "StoryMap",
        "Dashboard",
        "Hub Site Application",
        "Instant App",
    ]
    LAYER_TYPES = [
        "Feature Service",
        "Map Service",
        "Image Service",
        "Tile Layer",
        "Vector Tile Service",
        "WFS",
        "WMS",
    ]
    DATA_TYPES = [
        "CSV",
        "GeoJSON",
        "Shapefile",
        "File Geodatabase",
        "Web Map",
        "Web Scene",
    ]
    QUERYABLE_TYPES = {
        "Feature Service",
        "Map Service",
        "Feature Layer",
        "Table",
    }

    # On-prem MOA hosts we'll proxy without further checks. ArcGIS Online
    # hosts (*.arcgis.com) are handled separately in _validate_service_url:
    # they must either be this org's portal or carry the configured org_id
    # in the URL path, so we can't be coerced into proxying other tenants.
    ONPREM_HOST_SUFFIXES = (".muni.org",)

    ITEM_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[AnchorageGISPluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        # LRU {(item_id, group_by_field, agg_where): (expires_epoch, polygons)}.
        # Bounded to prevent memory exhaustion via agg_where variants that
        # miss the cache (e.g. "1=1 AND 1=1" vs "1=1 AND 2=2").
        self._agg_layer_cache: "OrderedDict[Tuple[str, str, str], Tuple[float, List[Dict[str, Any]]]]" = OrderedDict()

    async def initialize(self) -> bool:
        try:
            self.plugin_config = AnchorageGISPluginConfig(**self.config)
            self.client = httpx.AsyncClient(
                timeout=self.plugin_config.timeout,
            )

            # Test connectivity with a minimal search
            resp = await self.client.get(
                f"{self.plugin_config.portal_base_url}/search",
                params={
                    "q": f"orgid:{self.plugin_config.org_id}",
                    "f": "json",
                    "num": "1",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(
                    data["error"].get("message", str(data["error"]))
                )

            self._initialized = True
            logger.info(
                f"Anchorage GIS plugin initialized successfully for "
                f"{self.plugin_config.city_name}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to initialize Anchorage GIS plugin: {e}", exc_info=True
            )
            return False

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("Anchorage GIS plugin shut down")

    # ── Portal search helpers ─────────────────────────────────────────────

    async def _run_search(self, q: str, limit: int) -> List[Dict[str, Any]]:
        """Run a search against the ArcGIS Portal REST API."""
        params = {
            "q": q,
            "f": "json",
            "num": str(limit),
            "sortField": "relevance",
            "sortOrder": "desc",
        }
        resp = await self.client.get(
            f"{self.plugin_config.portal_base_url}/search",
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                data["error"].get("message", str(data["error"]))
            )
        return data.get("results", [])

    async def _search_gallery(
        self, query: str, limit: int
    ) -> List[Dict[str, Any]]:
        """Search within the curated gallery group."""
        clauses = [f"group:{self.plugin_config.gallery_group_id}"]
        if query:
            clauses.append(query)
        return await self._run_search(" AND ".join(clauses), limit)

    async def _search_org_layers(
        self, query: str, item_types: List[str], limit: int
    ) -> List[Dict[str, Any]]:
        """Search the organization's spatial layers.

        We scope the upstream query with ``orgid:<org_id>``; the
        post-filter below is defense-in-depth in case Esri ever returns
        results that don't honor that filter (e.g. shared items, cross-
        org content, indexing quirks).

        Items with ``orgId`` unset (None / empty) are kept -- many
        legitimate items in this tenant (FEMA-imported feeds, older
        items predating the field) have a null ``orgId`` in the
        response, even though the upstream ``orgid:`` query confirmed
        they belong to this org. The post-filter only rejects items
        whose ``orgId`` is set to a *different* organization, which is
        the actual cross-org leak we care about.
        """
        type_filter = " OR ".join(f'type:"{t}"' for t in item_types)
        clauses = [f"orgid:{self.plugin_config.org_id}", f"({type_filter})"]
        if query:
            clauses.append(query)
        results = await self._run_search(" AND ".join(clauses), limit)
        configured = (self.plugin_config.org_id or "").lower()
        kept: List[Dict[str, Any]] = []
        for r in results:
            item_org = (r.get("orgId") or "").lower()
            if not item_org or item_org == configured:
                kept.append(r)
        return kept

    # ── Formatters ────────────────────────────────────────────────────────

    @property
    def _portal_home(self) -> str:
        """Portal home URL (without /sharing/rest)."""
        return self.plugin_config.portal_base_url.replace("/sharing/rest", "")

    def _item_portal_url(self, item: Dict[str, Any]) -> str:
        url = item.get("url", "")
        item_type = item.get("type", "")
        item_id = item.get("id", "")
        if url and item_type in self.GALLERY_APP_TYPES:
            return url
        return f"{self._portal_home}/home/item.html?id={item_id}"

    def _format_summary(self, item: Dict[str, Any]) -> str:
        title = item.get("title", "Untitled")
        item_type = item.get("type", "Unknown")
        snippet = (item.get("snippet") or "").strip()
        tags = item.get("tags", [])
        item_id = item.get("id", "")
        url = self._item_portal_url(item)

        lines = [f"**{title}**  _{item_type}_ -- ID: `{item_id}`"]
        if snippet:
            lines.append(snippet)
        if tags:
            lines.append(f"Tags: {', '.join(tags[:6])}")
        lines.append(url)
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _ms_to_date(ms: Any) -> str:
        if ms:
            try:
                return datetime.fromtimestamp(
                    int(ms) / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except (ValueError, TypeError, OSError):
                pass
        return "Unknown"

    @staticmethod
    def _with_retrieved_footer(text: str) -> str:
        # Stamp every tool response with a UTC retrieval timestamp so
        # models can tell stale outputs from fresh ones. Skip if the
        # text already carries a Retrieved: line (query_data emits one
        # in its provenance header) to avoid double-stamping.
        if not text or "Retrieved:" in text:
            return text
        retrieved_at = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return f"{text}\n\n_Retrieved: {retrieved_at}_"

    @staticmethod
    def _ms_to_iso_smart(ms: Any) -> Any:
        # Midnight UTC -> date-only (preserves output for true date fields
        # like effective/inspection dates). Non-midnight -> full ISO so we
        # don't silently drop the time-of-day on real datetime fields.
        # On failure, return the raw value rather than a placeholder --
        # losing the data is worse than showing the epoch ms.
        if ms is None or ms == "":
            return ms
        try:
            dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return ms
        if dt.hour == dt.minute == dt.second == dt.microsecond == 0:
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _format_details(self, item: Dict[str, Any]) -> str:
        title = item.get("title", "Untitled")
        item_type = item.get("type", "Unknown")
        item_id = item.get("id", "")
        snippet = (item.get("snippet") or "").strip()
        description = (
            item.get("description") or "No description available."
        ).strip()
        tags = item.get("tags", [])
        categories = item.get("categories", [])
        owner = item.get("owner", "")
        access = item.get("access", "")
        url = item.get("url", "")
        num_views = item.get("numViews", 0)
        created = self._ms_to_date(item.get("created"))
        modified = self._ms_to_date(item.get("modified"))

        extent = item.get("extent") or []
        extent_str = ""
        if len(extent) == 2:
            try:
                extent_str = (
                    f"SW {extent[0][1]:.4f}\u00b0N {extent[0][0]:.4f}\u00b0E  "
                    f"NE {extent[1][1]:.4f}\u00b0N {extent[1][0]:.4f}\u00b0E"
                )
            except (IndexError, TypeError):
                pass

        lines = [
            f"## {title}",
            f"**Type:** {item_type}  |  **ID:** `{item_id}`",
            f"**Owner:** {owner}  |  **Access:** {access}  |  **Views:** {num_views:,}",
            f"**Created:** {created}  |  **Modified:** {modified}",
            "",
            "### Summary",
            snippet or "_No summary._",
            "",
            "### Description",
            description,
        ]
        if tags:
            lines += ["", f"**Tags:** {', '.join(tags)}"]
        if categories:
            lines += [f"**Categories:** {', '.join(categories)}"]
        if extent_str:
            lines += [f"**Spatial Extent:** {extent_str}"]
        if url:
            lines += [f"**Service/App URL:** {url}"]
        lines += [
            f"**Portal Page:** {self._portal_home}/home/item.html?id={item_id}"
        ]
        queryable = item_type in (
            "Feature Service",
            "Feature Layer",
            "Map Service",
            "Table",
        )
        lines += ["", "---"]
        if queryable:
            lines += [
                "**NEXT STEPS:** this is a queryable layer. Use "
                f"`query_data('{item_id}', limit=1)` to count records "
                f"(read the TOTAL COUNT line); "
                f"`get_layer_schema('{item_id}')` to see field names; "
                f"`query_data('{item_id}', where=..., limit=N)` to list."
            ]
        else:
            lines += [
                f"**NOTE:** type '{item_type}' is not directly queryable. "
                "It may bundle queryable layers -- open the Portal Page "
                "above to inspect, or use `find_gis_content` to find a "
                "related Feature Service."
            ]
        return "\n".join(lines)

    # Upper bound on how many chars of a single feature's geometry we
    # dump into the response. Simplified polygons are usually well
    # under this; anything larger gets truncated with a clear marker.
    GEOMETRY_STR_MAX = 600

    # Devil's-advocate thresholds.
    STALENESS_THRESHOLD_DAYS = 365  # layer unchanged > 1 yr -> freshness note
    COVERAGE_THRESHOLD = 0.5  # < 50% muni overlap -> coverage note
    SMALL_SAMPLE_THRESHOLD = 10  # total_count < 10 (and > 1) -> small-N note

    def _format_query_results(
        self,
        records: List[Dict[str, Any]],
        limit: int,
        total_count: Optional[int] = None,
        date_fields: Optional[set] = None,
        geometry_type: Optional[str] = None,
        name_field: Optional[str] = None,
        item_id: Optional[str] = None,
        service_url: Optional[str] = None,
        where: Optional[str] = None,
        out_fields: Optional[str] = None,
        coded_domains: Optional[Dict[str, Dict[Any, str]]] = None,
        last_edit_date: Optional[int] = None,
        coverage_pct: Optional[float] = None,
    ) -> str:
        provenance: List[str] = []
        if service_url:
            provenance.append(f"Source: {service_url}")
        if where is not None or out_fields is not None:
            parts = []
            if where is not None:
                parts.append(f"where={where!r}")
            if out_fields is not None:
                parts.append(f"outFields={out_fields!r}")
            parts.append(f"resultRecordCount={limit}")
            provenance.append(f"Query: {', '.join(parts)}")
        if provenance:
            retrieved_at = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            provenance.append(f"Retrieved: {retrieved_at}")

        # Truncation banner: make it impossible to miss that the listed
        # records are a sample. Only fire when total_count proves the
        # cap; for non-paginating callers (spatial_*) total_count is
        # None and we stay silent.
        truncated = (
            total_count is not None
            and len(records) > 0
            and total_count > len(records)
        )
        if truncated:
            provenance.append(
                f"**TRUNCATED:** returned {len(records)} of "
                f"{total_count:,} matching records (limit={limit}). "
                f"The records below are a SAMPLE -- do not generalize "
                f"counts or percentages from them. Use the TOTAL COUNT "
                f"line below for 'how many?' questions, or narrow the "
                f"WHERE clause to get a complete listing."
            )

        # Single-record / small-sample caveats: fire only when
        # total_count was actually computed and the result is small
        # enough that a model could mistakenly extrapolate a trend.
        if total_count == 1:
            provenance.append(
                "**SINGLE-RECORD CLAIM:** only 1 matching record. Do "
                "not report this as a trend, pattern, or "
                "distribution -- it is an N=1 anecdote."
            )
        elif (
            total_count is not None
            and 1 < total_count < self.SMALL_SAMPLE_THRESHOLD
        ):
            provenance.append(
                f"**SMALL SAMPLE:** only {total_count} matching "
                f"records. Percentages, distributions, and trend "
                f"claims drawn from this set are weak -- name the "
                f"sample size in any summary."
            )

        # Staleness caveat: layer hasn't been edited in a long time.
        # The threshold is intentionally a year (not days) -- false
        # positives here are cheap (informational), false negatives
        # (calling old data "current") are expensive.
        if last_edit_date:
            try:
                edit_dt = datetime.fromtimestamp(
                    int(last_edit_date) / 1000, tz=timezone.utc
                )
                age_days = (datetime.now(timezone.utc) - edit_dt).days
                if age_days > self.STALENESS_THRESHOLD_DAYS:
                    age_years = age_days / 365.25
                    provenance.append(
                        f"**DATA FRESHNESS:** layer last edited "
                        f"{edit_dt.strftime('%Y-%m-%d')} "
                        f"({age_years:.1f} years ago). Confirm this "
                        f"matches the recency the question needs."
                    )
            except (ValueError, TypeError, OSError):
                pass

        # Coverage caveat: layer's spatial extent covers a small slice
        # of Anchorage. Skip when coverage_pct is None (unhandled SR);
        # honest silence beats a guessed flag.
        if coverage_pct is not None:
            if coverage_pct == 0.0:
                provenance.append(
                    "**COVERAGE:** this layer's spatial extent does "
                    "not overlap Anchorage at all. Confirm this is "
                    "the right layer."
                )
            elif coverage_pct < self.COVERAGE_THRESHOLD:
                pct_int = max(1, int(round(coverage_pct * 100)))
                provenance.append(
                    f"**LIMITED COVERAGE:** this layer's extent "
                    f"covers ~{pct_int}% of Anchorage. Confirm the "
                    f"question's area of interest falls inside the "
                    f"layer's coverage."
                )

        if not records:
            if provenance:
                return "\n".join(provenance + ["", "No records returned."])
            return "No records returned."

        count_part = f"{len(records)}"
        if total_count is not None:
            count_part += f" of {total_count:,} total"
        lines = list(provenance)
        if provenance:
            lines.append("")
        lines.append(f"Returned {count_part} record(s) (limit: {limit}).")
        if total_count is not None:
            lines.append(
                f"TOTAL COUNT (records matching the WHERE clause): "
                f"{total_count:,}. "
                f"This is the answer to 'how many?' -- use it directly "
                f"instead of counting the records below."
            )
        # Polyline-grain warning: a single named entity (a trail, a
        # road, a transit route) is typically stored as MULTIPLE line
        # segments. Reporting "1,123 trails" when the layer holds
        # 1,123 segments is wrong -- there might be 200 unique named
        # trails. Surface this whenever the layer is polyline-typed
        # AND a count was requested, so the model frames its answer
        # honestly. If we know a likely name field, suggest the
        # follow-up call to count unique entities.
        if (
            geometry_type == "esriGeometryPolyline"
            and total_count is not None
        ):
            id_arg = (
                f"item_id='{item_id}'" if item_id else "item_id=<id>"
            )
            if name_field:
                follow_up = (
                    f"`get_distinct_values({id_arg}, "
                    f"field='{name_field}', limit=500)` and count "
                    f"the distinct values returned"
                )
            else:
                follow_up = (
                    f"`get_layer_schema({id_arg})` to find the "
                    f"name/identifier field, then "
                    f"`get_distinct_values` on it to count unique "
                    f"entities"
                )
            lines.append(
                f"**GRAIN NOTE (polyline layer):** the count above "
                f"is the number of LINE SEGMENTS (geometry features), "
                f"NOT the number of unique named entities. A single "
                f"named trail, road, or route is typically stored as "
                f"multiple connected segments -- so '{total_count:,} "
                f"records' usually means fewer than {total_count:,} "
                f"distinct named features. When answering the user, "
                f"say e.g. '{total_count:,} trail segments' rather "
                f"than '{total_count:,} trails'. To count unique "
                f"named entities, call {follow_up}."
            )
        lines.append("")
        for i, record in enumerate(records, 1):
            lines.append(f"Record {i}:")
            geometry = record.get("__geometry__")
            for key, value in record.items():
                if key == "__geometry__":
                    continue
                if date_fields and key in date_fields and value is not None:
                    value = self._ms_to_iso_smart(value)
                elif (
                    coded_domains
                    and key in coded_domains
                    and value is not None
                    and value in coded_domains[key]
                ):
                    # Render coded values as "<code> (<label>)" so the
                    # model doesn't have to guess what e.g. R1A means.
                    # Skip the parenthetical when code and label are
                    # the same string to avoid noise like
                    # "Status: Open (Open)".
                    label = coded_domains[key][value]
                    if str(value) != str(label):
                        value = f"{value} ({label})"
                lines.append(f"  {key}: {value}")
            if geometry is not None:
                geom_str = json.dumps(geometry, separators=(",", ":"))
                if len(geom_str) > self.GEOMETRY_STR_MAX:
                    geom_clip = geom_str[: self.GEOMETRY_STR_MAX]
                    geom_str = (
                        f"{geom_clip}... "
                        f"(truncated, {len(geom_str)} chars total; "
                        f"server-side simplified to "
                        f"~{self.GEOMETRY_SIMPLIFY_OFFSET_DEG} deg "
                        f"~ 5.5m)"
                    )
                lines.append(f"  geometry (GeoJSON, WGS84): {geom_str}")
            lines.append("")
        # The TRUNCATED banner and TOTAL COUNT line above already carry
        # the sampling caveat. Copilot (now GPT-5.1) and Claude both read
        # the full response top-down, so the older bottom-of-response
        # restatement that guarded against GPT-4o-style summarization is
        # no longer needed and has been dropped.
        return "\n".join(lines)

    # ── DataPlugin interface methods ──────────────────────────────────────

    async def search_datasets(
        self, query: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Search gallery and org layers for a topic."""
        gallery, layers = await asyncio.gather(
            self._search_gallery(query, limit),
            self._search_org_layers(
                query, self.LAYER_TYPES + self.DATA_TYPES, limit
            ),
        )
        return gallery + layers

    async def get_dataset(self, dataset_id: str) -> Dict[str, Any]:
        """Get item details by ArcGIS item ID.

        Rejects items not owned by the configured org. ArcGIS portals
        will happily return any *public* item from any org, so without
        this check a caller could browse arbitrary ArcGIS Online content
        through this MCP and use item descriptions as a prompt-injection
        vector against the calling LLM.
        """
        dataset_id = self._validate_item_id(dataset_id)
        resp = await self.client.get(
            f"{self.plugin_config.portal_base_url}/content/items/{dataset_id}",
            params={"f": "json"},
        )
        resp.raise_for_status()
        item = resp.json()
        if "error" in item:
            raise RuntimeError(
                item["error"].get("message", str(item["error"]))
            )
        self._assert_owned_by_configured_org(item)
        return item

    def _assert_owned_by_configured_org(self, item: Dict[str, Any]) -> None:
        """Fail-closed ownership check. Missing orgId is also a rejection."""
        configured = (self.plugin_config.org_id or "").lower()
        item_org = (item.get("orgId") or "").lower()
        if not item_org or item_org != configured:
            raise ValueError(
                f"Item {item.get('id')!r} belongs to org "
                f"{item.get('orgId')!r}, not the configured org "
                f"{self.plugin_config.org_id!r}; this MCP only serves "
                f"{self.plugin_config.city_name} data."
            )

    @staticmethod
    def _rewrite_arcgis_error(
        msg: str,
        details: List[str],
        resource_id: Optional[str] = None,
        has_out_fields: bool = False,
        has_where: bool = False,
    ) -> str:
        """Turn raw ArcGIS REST errors into actionable instructions.

        ArcGIS error messages assume a developer audience that knows
        the schema and the REST contract. A weaker model reading
        "Cannot perform query. Invalid query parameters." has no idea
        what to do next. We pattern-match the common shapes and append
        the concrete tool call the model should make to recover.
        """
        msg = msg or "Unknown error"
        detail_str = "; ".join(details) if details else ""
        full = f"{msg}" + (f" -- {detail_str}" if detail_str else "")
        item_arg = (
            f"item_id='{resource_id}'" if resource_id else "item_id=<id>"
        )
        # Case A: ArcGIS named the bad field (WHERE-clause case).
        m = re.search(
            r"[Ii]nvalid\s+field\s*:\s*([A-Za-z0-9_]+)", full
        )
        if m:
            bad = m.group(1)
            return (
                f"Field '{bad}' does not exist on this layer. "
                f"Field names are CASE-SENSITIVE. To recover: call "
                f"`get_layer_schema({item_arg})` to see valid field "
                f"names, then retry with the exact name shown there. "
                f"(Underlying error: {full})"
            )
        # Case B: generic "Invalid query parameters" -- usually
        # out_fields contains a name that doesn't exist. ArcGIS does
        # NOT echo the bad name back, so the model has to discover it.
        if "Invalid query parameters" in full:
            hint_parts = []
            if has_out_fields:
                hint_parts.append(
                    "out_fields may reference a field that does not "
                    "exist (ArcGIS does not name it in the error). "
                    "Try out_fields='*' to confirm, then narrow."
                )
            if has_where:
                hint_parts.append(
                    "the WHERE clause may reference a missing field "
                    "or use the wrong type (string values must be "
                    "single-quoted)."
                )
            hint_parts.append(
                f"Call `get_layer_schema({item_arg})` to see valid "
                f"field names -- they are CASE-SENSITIVE."
            )
            return f"{full}\n\nLikely cause: " + " ".join(hint_parts)
        return full

    @staticmethod
    def _no_data_hint(where_clause: str) -> str:
        """Hint to append after an empty result with a non-trivial WHERE.

        Any model can write ``Field='exact value'`` when what it wanted
        is ``Field LIKE '%substring%'``. ArcGIS returns zero rows
        silently, and without a nudge the model reports the data does
        not exist. Append a concrete recovery instruction so the model
        retries instead of giving up.
        """
        normalized = (where_clause or "").strip()
        if not normalized or normalized == "1=1":
            return ""
        return (
            "\n\n_If you expected matches:_\n"
            "- For TEXT fields, exact-match `=` is strict and "
            "case-sensitive. Try `Field LIKE '%substring%'` "
            "(% is the wildcard).\n"
            "- For NUMERIC/DATE fields, verify the value type "
            "matches the schema.\n"
            "- Field names are CASE-SENSITIVE -- call "
            "`get_layer_schema(item_id=<id>)` to confirm.\n"
            "- To verify the layer has data, retry with "
            "`where='1=1'`."
        )

    # Field-name priority for picking the user-facing identifier from
    # a feature's attributes. A model can latch onto the first
    # identifier-shaped value it sees in the response and report it back
    # as canonical -- if the lead line says "OBJECTID 778" the model may
    # say "parcel 778" even when Parcel_ID is right below. We promote one
    # of these field names into the lead position so the wrong identifier
    # never appears first, rather than relying on the model to sort it
    # out from a warning.
    NATURAL_ID_FIELD_PRIORITY = (
        # Parcel-style identifiers (most common request shape).
        "Parcel_ID", "PARCEL_ID", "ParcelID", "PARCELID",
        "Parcel_Num", "PARCEL_NUM", "ParcelNum", "PARCELNUM",
        "Parcel_Number", "ParcelNumber",
        "GIS_ParcelNum8Formatted", "GIS_ParcelNum11Formatted",
        "GIS_ParcelNum8", "GIS_ParcelNum11",
        # Polyline-entity name fields -- listed BEFORE generic `Name`
        # so a trails/roads layer with both `Trail_Name` and `Name`
        # picks the more specific one. These also drive the grain
        # warning's follow-up suggestion when query_data hits a
        # polyline layer.
        "Trail_Name", "TRAIL_NAME", "TrailName",
        "Road_Name", "ROAD_NAME", "RoadName",
        "Street_Name", "STREET_NAME", "StreetName",
        "Route_Name", "ROUTE_NAME", "RouteName",
        # Generic record-name fields (next-best fallback).
        "Name", "NAME", "FullName", "Full_Name",
        "Site_Name", "SiteName",
        "Site_Address", "Address", "ADDRESS",
        "Title", "TITLE",
        # Common code/identifier fields.
        "Permit_Number", "PermitNumber",
        "Project_Name", "ProjectName",
        "Plat_Number", "PlatNumber",
    )

    # Subset of NATURAL_ID_FIELD_PRIORITY that strongly indicates a
    # layer is at parcel grain (one row per legal parcel). Used to
    # detect when a classification arg is the wrong shape -- e.g., a
    # parcels-with-Zoning_District-attribute layer being used as the
    # classification for "parcels spanning multiple zones". The right
    # classification is a zoning-polygon layer where many polygons
    # share each zone code.
    PARCEL_INDICATOR_FIELDS = frozenset((
        "Parcel_ID", "PARCEL_ID", "ParcelID", "PARCELID",
        "Parcel_Num", "PARCEL_NUM", "ParcelNum", "PARCELNUM",
        "Parcel_Number", "ParcelNumber",
        "GIS_ParcelNum8", "GIS_ParcelNum11",
        "GIS_ParcelNum8Formatted", "GIS_ParcelNum11Formatted",
    ))

    @classmethod
    def _pick_natural_id(
        cls, attrs: Dict[str, Any]
    ) -> Optional[Tuple[str, Any]]:
        """Pick the most likely user-facing identifier from a feature's
        attributes. Returns (field_name, value) or None.

        Priority is: parcel-ish IDs > name-ish fields > misc codes.
        Skips None/empty values so the lead line never says
        "Parcel_ID None".
        """
        for field in cls.NATURAL_ID_FIELD_PRIORITY:
            if field in attrs:
                v = attrs[field]
                if v is not None and v != "":
                    return (field, v)
        return None

    @staticmethod
    def _normalize_parcel_variants(raw: Any) -> List[str]:
        """Generate MOA parcel ID format variants for cross-dataset
        lookup.

        MOA parcel IDs are stored in two related canonical forms across
        layers:
          - 8-digit base: ``XXXXXXXX`` (compact) or ``XXX-XXX-XX``
            (hyphenated) -- e.g. ``00318487`` / ``003-184-87``.
          - 11-digit extended: 8-digit base + 3-digit sub-parcel
            suffix (``000`` means no sub) -- e.g. ``00318487000`` /
            ``003-184-87-000``.

        TaxParcels stores 11-digit compact in ``Parcel_Num``/``Name``;
        PropertyInformation has all four variants in separate columns
        (``GIS_ParcelNum8``, ``GIS_ParcelNum8Formatted``,
        ``GIS_ParcelNum11``, ``GIS_ParcelNum11Formatted``). The model
        rarely knows which form a given layer uses, so we generate
        all four for use in ``WHERE field IN (...)``.

        Input handling: extracts digits from the input, pads/splits
        based on length to recover the 8-digit base + 3-digit sub.
        Hyphens, leading zeros, and prefixes/suffixes are flexible.
        """
        if raw is None:
            return []
        digits = "".join(c for c in str(raw) if c.isdigit())
        if not digits or len(digits) < 5:
            return []

        if len(digits) >= 11:
            # Take the LAST 11 digits -- accommodates inputs like
            # "Parcel 00318487000" if any non-digit prefixes slipped
            # through.
            tail = digits[-11:]
            base8 = tail[:8]
            sub3 = tail[8:11]
        elif len(digits) >= 9:
            # 9 or 10 digits -- pad on the left to 11, then split.
            padded = digits.rjust(11, "0")
            base8 = padded[:8]
            sub3 = padded[8:11]
        else:
            # 5-8 digits -- pad on the left to 8, default to no
            # sub-parcel.
            base8 = digits.rjust(8, "0")
            sub3 = "000"

        variants: set = set()
        variants.add(base8)
        variants.add(f"{base8[0:3]}-{base8[3:6]}-{base8[6:8]}")
        variants.add(base8 + sub3)
        variants.add(
            f"{base8[0:3]}-{base8[3:6]}-{base8[6:8]}-{sub3}"
        )
        # Always also try the literal stripped input, in case the layer
        # stores some non-canonical form we did not anticipate.
        literal = str(raw).strip()
        if literal:
            variants.add(literal)
        return sorted(variants)

    @staticmethod
    def _not_queryable_message(
        item_id: str, item_type: str = ""
    ) -> str:
        type_note = (
            f" (item type: '{item_type}')" if item_type else ""
        )
        return (
            f"Item '{item_id}' is not a queryable Feature/Map "
            f"Service{type_note}. Web Maps, Apps, Dashboards, and "
            f"Story Maps are VIEWERS, not data -- they cannot be "
            f"queried for records. To recover: call "
            f"`find_gis_content(topic=<your topic>)` and pick from "
            f"the **QUERYABLE** section (Feature/Map Services), or "
            f"`get_item_details(item_id='{item_id}')` to inspect "
            f"this item's relationships."
        )

    # Cap on records when geometry is requested -- polygons can be
    # orders of magnitude larger than attribute rows, so we keep this
    # much tighter than the no-geometry cap of 1000.
    GEOMETRY_LIMIT_CAP = 50

    # Server-side simplification tolerance in the output SR's units.
    # We pin outSR=4326 when returnGeometry=true, so this is in decimal
    # degrees: 0.00005 deg ~ 5.5m at the equator. Fine for MCP-scale
    # reasoning about shape, keeps payloads manageable.
    GEOMETRY_SIMPLIFY_OFFSET_DEG = 0.00005

    async def query_data(
        self,
        resource_id: str,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        return_geometry: bool = False,
    ) -> List[Dict[str, Any]]:
        """Query records from a Feature Service by item ID.

        Two-hop resolution: looks up item to get service URL, then queries it.

        When return_geometry=True, the response format switches to GeoJSON
        (f=geojson), geometry is auto-simplified to
        ~GEOMETRY_SIMPLIFY_OFFSET_DEG, and `limit` is capped at
        GEOMETRY_LIMIT_CAP. Each returned record carries a `__geometry__`
        key holding the GeoJSON geometry object.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        item = await self.get_dataset(resource_id)
        service_url = item.get("url", "")
        item_type = item.get("type", "")

        if not service_url:
            raise ValueError(
                self._not_queryable_message(resource_id, item_type)
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                self._not_queryable_message(resource_id, item_type)
            )

        where_clause = filters.get("where", "1=1") if filters else "1=1"
        where_clause = WhereValidator.validate(where_clause)
        raw_out_fields = filters.get("out_fields", "*") if filters else "*"
        out_fields = OutFieldsValidator.validate(raw_out_fields)
        order_by = OrderByValidator.validate(
            filters.get("order_by") or "" if filters else ""
        )

        service_url = self._ensure_layer_url(service_url)
        self._validate_service_url(service_url)
        query_url = f"{service_url}/query"

        max_records = (
            self.GEOMETRY_LIMIT_CAP if return_geometry else 1000
        )
        effective_limit = min(limit, max_records)
        params: Dict[str, Any] = {
            "where": where_clause,
            "outFields": out_fields,
            "resultRecordCount": effective_limit,
            "f": "geojson" if return_geometry else "json",
            "returnGeometry": "true" if return_geometry else "false",
        }
        if return_geometry:
            # Pin output SR so callers can't swap coordinate systems;
            # keeps the simplification offset in known units.
            params["outSR"] = "4326"
            params["maxAllowableOffset"] = str(
                self.GEOMETRY_SIMPLIFY_OFFSET_DEG
            )
        if order_by:
            params["orderByFields"] = order_by

        try:
            resp = await self.client.get(query_url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service query error (HTTP {e.response.status_code}): "
                f"{e.response.text}"
            ) from e

        try:
            data = resp.json()
        except Exception as json_err:
            content_type = resp.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type}). The item URL may not "
                f"point to a queryable ArcGIS Feature Service."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            raise RuntimeError(
                f"Feature Service query failed (code {code}): "
                + self._rewrite_arcgis_error(
                    msg,
                    details,
                    resource_id=resource_id,
                    has_out_fields=raw_out_fields not in ("*", ""),
                    has_where=where_clause not in ("1=1", ""),
                )
            )

        features = data.get("features", [])
        if return_geometry:
            # f=geojson returns FeatureCollection with
            # {type,geometry,properties} features.
            return [
                {
                    **(f.get("properties") or {}),
                    "__geometry__": f.get("geometry"),
                }
                for f in features
            ]
        return [f.get("attributes", {}) for f in features]

    async def spatial_query_point(
        self,
        resource_id: str,
        lon: float,
        lat: float,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find polygon features in a Feature Service that contain a point.

        Uses ArcGIS REST spatialRel=Intersects with a point geometry in
        WGS84 (EPSG:4326). Input SR is pinned server-side so callers
        cannot supply an arbitrary CRS. Returns attributes only; geometry
        is suppressed to keep payloads small.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        lon_f, lat_f = self._validate_lonlat(lon, lat)

        item = await self.get_dataset(resource_id)
        service_url = item.get("url", "")
        item_type = item.get("type", "")

        if not service_url:
            raise ValueError(
                self._not_queryable_message(resource_id, item_type)
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                self._not_queryable_message(resource_id, item_type)
            )

        raw_where = (filters or {}).get("where", "1=1")
        raw_out_fields = (filters or {}).get("out_fields", "*")
        where_clause = WhereValidator.validate(raw_where)
        out_fields = OutFieldsValidator.validate(raw_out_fields)

        service_url = self._ensure_layer_url(service_url)
        self._validate_service_url(service_url)

        # Pre-check: layer must be polygon-type for point-in-polygon
        # to make sense. Fail loudly rather than silently returning
        # whatever Intersects happens to hit on a points/lines layer.
        meta_resp = await self.client.get(
            service_url, params={"f": "json"}
        )
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        if "error" in meta:
            raise RuntimeError(
                meta["error"].get("message", str(meta["error"]))
            )
        geom_type = meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"spatial_query_point requires a polygon layer "
                f"(layer geometryType is {geom_type or 'unknown'!r})"
            )

        query_url = f"{service_url}/query"
        params = {
            "where": where_clause,
            "outFields": out_fields,
            "geometry": f"{lon_f},{lat_f}",
            "geometryType": "esriGeometryPoint",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "resultRecordCount": min(limit, 50),
            "returnGeometry": "false",
            "f": "json",
        }

        try:
            resp = await self.client.get(query_url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service spatial query error "
                f"(HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        try:
            data = resp.json()
        except Exception as json_err:
            content_type = resp.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type})."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            raise RuntimeError(
                f"Feature Service spatial query failed (code {code}): "
                + self._rewrite_arcgis_error(
                    msg,
                    details,
                    resource_id=resource_id,
                    has_out_fields=raw_out_fields not in ("*", ""),
                    has_where=raw_where not in ("1=1", ""),
                )
            )

        features = data.get("features", [])
        return [f.get("attributes", {}) for f in features]

    _SPATIAL_REL_MAP = {
        "intersects": "esriSpatialRelIntersects",
        "contains": "esriSpatialRelContains",
        "within": "esriSpatialRelWithin",
        "crosses": "esriSpatialRelCrosses",
        "touches": "esriSpatialRelTouches",
        "overlaps": "esriSpatialRelOverlaps",
        "envelope_intersects": "esriSpatialRelEnvelopeIntersects",
    }

    # Linear units accepted from the model (case-insensitive, with common
    # aliases) and how they map to (a) the ArcGIS REST `units` constant for
    # server-side buffered queries, and (b) meters for client-side distance
    # math in aggregate_by_polygon. Keep the two maps in lock-step.
    _LINEAR_UNIT_ALIASES = {
        "meters": "meters", "meter": "meters", "metre": "meters",
        "metres": "meters", "m": "meters",
        "kilometers": "kilometers", "kilometer": "kilometers",
        "kilometre": "kilometers", "kilometres": "kilometers",
        "km": "kilometers",
        "feet": "feet", "foot": "feet", "ft": "feet",
        "miles": "miles", "mile": "miles", "mi": "miles",
        "yards": "yards", "yard": "yards", "yd": "yards",
    }
    _ESRI_LINEAR_UNITS = {
        "meters": "esriSRUnit_Meter",
        "kilometers": "esriSRUnit_Kilometer",
        "feet": "esriSRUnit_Foot",
        "miles": "esriSRUnit_StatuteMile",
        "yards": "esriSRUnit_Yard",
    }
    _LINEAR_UNIT_TO_METERS = {
        "meters": 1.0,
        "kilometers": 1000.0,
        "feet": 0.3048,
        "miles": 1609.344,
        "yards": 0.9144,
    }

    @classmethod
    def _normalize_linear_unit(cls, units: Optional[str]) -> str:
        """Canonicalize a free-text linear unit (default meters)."""
        key = (units or "meters").strip().lower()
        canonical = cls._LINEAR_UNIT_ALIASES.get(key)
        if canonical is None:
            raise ValueError(
                f"units {units!r} is not a supported linear unit. Use "
                f"one of: meters, kilometers, feet, miles, yards."
            )
        return canonical

    async def spatial_query_polygon(
        self,
        resource_id: str,
        filter_geometry: Optional[Dict[str, Any]] = None,
        filter_item_id: Optional[str] = None,
        filter_where: str = "1=1",
        spatial_rel: str = "intersects",
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 50,
        return_geometry: bool = False,
        distance: Optional[float] = None,
        units: str = "meters",
    ) -> List[Dict[str, Any]]:
        """Find features in a target layer that spatially relate to a polygon filter.

        The filter polygon is supplied either inline as GeoJSON via
        ``filter_geometry`` (Polygon, MultiPolygon, or Feature), or by
        dereferencing polygon features in another layer via
        ``filter_item_id`` + ``filter_where``. The target layer may be
        polygon, polyline, or point.

        Unlike a centroid-based assignment, this returns every target
        feature whose geometry touches the filter polygon, so features
        that straddle the filter boundary are still included. Set
        ``return_geometry=True`` to get GeoJSON geometries back for
        precise client-side clipping.

        When ``distance`` is a positive number, the filter polygon is
        buffered by that distance (in ``units``) server-side before the
        spatial relation is evaluated -- this is how "within N miles/feet
        of <polygon>" proximity questions are answered (e.g. population
        within half a mile of a park).
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1 (got {limit})")

        buffer_distance: Optional[float] = None
        esri_units: Optional[str] = None
        if distance is not None and str(distance) != "":
            try:
                buffer_distance = float(distance)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"distance must be a number (got {distance!r})"
                ) from exc
            if buffer_distance < 0:
                raise ValueError(
                    f"distance must be >= 0 (got {buffer_distance})"
                )
            if buffer_distance == 0:
                buffer_distance = None  # no buffer; treat as exact
            else:
                esri_units = self._ESRI_LINEAR_UNITS[
                    self._normalize_linear_unit(units)
                ]

        if not filter_geometry and not filter_item_id:
            raise ValueError(
                "spatial_query_polygon requires either filter_geometry "
                "(inline GeoJSON) or filter_item_id + filter_where"
            )

        spatial_rel_esri = self._SPATIAL_REL_MAP.get(
            (spatial_rel or "intersects").lower()
        )
        if not spatial_rel_esri:
            raise ValueError(
                f"spatial_rel must be one of "
                f"{sorted(self._SPATIAL_REL_MAP)} (got {spatial_rel!r})"
            )

        if filter_geometry:
            esri_filter = self._geojson_to_esri_polygon(filter_geometry)
        else:
            esri_filter = await self._fetch_filter_polygon(
                filter_item_id, filter_where
            )

        item = await self.get_dataset(resource_id)
        target_url = item.get("url", "")
        item_type = item.get("type", "")
        if not target_url:
            raise ValueError(
                self._not_queryable_message(resource_id, item_type)
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                self._not_queryable_message(resource_id, item_type)
            )

        target_url = self._ensure_layer_url(target_url)
        self._validate_service_url(target_url)

        raw_where = (filters or {}).get("where", "1=1")
        raw_out_fields = (filters or {}).get("out_fields", "*")
        where_clause = WhereValidator.validate(raw_where)
        out_fields = OutFieldsValidator.validate(raw_out_fields)

        max_records = (
            self.GEOMETRY_LIMIT_CAP if return_geometry else 1000
        )
        effective_limit = min(limit, max_records)

        params: Dict[str, Any] = {
            "where": where_clause,
            "outFields": out_fields,
            "geometry": json.dumps(esri_filter, separators=(",", ":")),
            "geometryType": "esriGeometryPolygon",
            "inSR": "4326",
            "spatialRel": spatial_rel_esri,
            "resultRecordCount": str(effective_limit),
            "f": "geojson" if return_geometry else "json",
            "returnGeometry": "true" if return_geometry else "false",
        }
        # Server-side buffer: ArcGIS expands the filter geometry by
        # `distance` (in `units`) before applying spatialRel, so a plain
        # parks polygon answers "within half a mile of a park".
        if buffer_distance is not None:
            params["distance"] = str(buffer_distance)
            params["units"] = esri_units
        if return_geometry:
            params["outSR"] = "4326"
            params["maxAllowableOffset"] = str(
                self.GEOMETRY_SIMPLIFY_OFFSET_DEG
            )

        query_url = f"{target_url}/query"
        try:
            # POST because filter polygons routinely exceed URL length
            # limits -- spatial_query_point can get away with GET, this
            # one cannot.
            resp = await self.client.post(query_url, data=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"Feature Service spatial query error "
                f"(HTTP {e.response.status_code}): {e.response.text}"
            ) from e

        try:
            data = resp.json()
        except Exception as json_err:
            content_type = resp.headers.get("content-type", "")
            raise ValueError(
                f"Feature Service returned non-JSON response "
                f"(content-type: {content_type})."
            ) from json_err

        error_in_body = data.get("error")
        if error_in_body:
            code = error_in_body.get("code", "unknown")
            msg = error_in_body.get("message", "Unknown error")
            details = error_in_body.get("details", [])
            raise RuntimeError(
                f"Feature Service spatial query failed (code {code}): "
                + self._rewrite_arcgis_error(
                    msg,
                    details,
                    resource_id=resource_id,
                    has_out_fields=raw_out_fields not in ("*", ""),
                    has_where=raw_where not in ("1=1", ""),
                )
            )

        features = data.get("features", [])
        if return_geometry:
            return [
                {
                    **(f.get("properties") or {}),
                    "__geometry__": f.get("geometry"),
                }
                for f in features
            ]
        return [f.get("attributes", {}) for f in features]

    # Caps on inbound filter polygons. ArcGIS will accept far larger geometries,
    # but huge inputs translate into huge POST bodies upstream and slow
    # spatial-query plans. Real Anchorage admin boundaries (council districts,
    # parks, plats) sit well under these limits. Raise with evidence.
    MAX_FILTER_RINGS = 1000
    MAX_FILTER_COORDS = 10000

    @staticmethod
    def _geojson_to_esri_polygon(geojson: Any) -> Dict[str, Any]:
        """Convert GeoJSON Polygon / MultiPolygon / Feature to Esri polygon JSON."""
        if not isinstance(geojson, dict):
            raise ValueError(
                f"filter_geometry must be a GeoJSON object "
                f"(got {type(geojson).__name__})"
            )
        gj_type = geojson.get("type", "")
        if gj_type == "Feature":
            return AnchorageGISPlugin._geojson_to_esri_polygon(
                geojson.get("geometry") or {}
            )
        if gj_type == "Polygon":
            rings = list(geojson.get("coordinates") or [])
        elif gj_type == "MultiPolygon":
            rings = []
            for poly in geojson.get("coordinates") or []:
                rings.extend(poly)
        else:
            raise ValueError(
                f"filter_geometry must be a GeoJSON Polygon, "
                f"MultiPolygon, or Feature wrapping one "
                f"(got type={gj_type!r})"
            )
        if not rings:
            raise ValueError("filter_geometry has no polygon rings")

        ring_count = len(rings)
        if ring_count > AnchorageGISPlugin.MAX_FILTER_RINGS:
            raise ValueError(
                f"filter_geometry has {ring_count} rings; "
                f"max is {AnchorageGISPlugin.MAX_FILTER_RINGS}. "
                f"Simplify the polygon or use filter_item_id with a "
                f"published boundary layer."
            )
        coord_count = sum(len(r) for r in rings if isinstance(r, list))
        if coord_count > AnchorageGISPlugin.MAX_FILTER_COORDS:
            raise ValueError(
                f"filter_geometry has {coord_count} coordinates; "
                f"max is {AnchorageGISPlugin.MAX_FILTER_COORDS}. "
                f"Simplify the polygon (e.g. mapshaper at 1% tolerance) "
                f"or use filter_item_id with a published boundary layer."
            )

        return {
            "rings": rings,
            "spatialReference": {"wkid": 4326},
        }

    async def _fetch_filter_polygon(
        self, filter_item_id: str, filter_where: str
    ) -> Dict[str, Any]:
        """Resolve a polygon filter from feature(s) in another layer.

        Queries the filter layer with ``filter_where``, validates it is a
        polygon layer, and unions all matching features' rings into a
        single Esri polygon. Typical use: pick one district or one park
        feature as the filter geometry for a target layer.
        """
        filter_item_id = self._validate_item_id(filter_item_id)
        validated_where = WhereValidator.validate(filter_where or "1=1")

        item = await self.get_dataset(filter_item_id)
        url = item.get("url", "")
        if not url:
            raise ValueError(
                f"filter_item_id {filter_item_id} has no service URL"
            )
        url = self._ensure_layer_url(url)
        self._validate_service_url(url)

        meta_resp = await self.client.get(url, params={"f": "json"})
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        if "error" in meta:
            raise RuntimeError(
                meta["error"].get("message", str(meta["error"]))
            )
        geom_type = meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"filter_item_id must point at a polygon layer "
                f"(got geometryType={geom_type or 'unknown'!r})"
            )

        query_url = f"{url}/query"
        params = {
            "where": validated_where,
            "outFields": "",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
            "resultRecordCount": "50",
        }
        resp = await self.client.post(query_url, data=params)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                data["error"].get("message", str(data["error"]))
            )

        features = data.get("features", [])
        if not features:
            raise ValueError(
                f"filter_where {validated_where!r} matched no features "
                f"in filter layer {filter_item_id}"
            )

        all_rings: List[Any] = []
        for f in features:
            geom = f.get("geometry") or {}
            all_rings.extend(geom.get("rings") or [])

        if not all_rings:
            raise ValueError(
                f"filter features in {filter_item_id} have no "
                f"polygon rings"
            )

        return {
            "rings": all_rings,
            "spatialReference": {"wkid": 4326},
        }

    # ── Additional tool implementations ───────────────────────────────────

    async def _find_gis_content(self, args: Dict[str, Any]) -> str:
        """Combined search: gallery + spatial layers."""
        topic = args.get("topic", "").strip()
        if not topic:
            raise ValueError(
                "topic is required -- pass the subject of the user's "
                "question as a 1-2 word topic (e.g. 'parks', 'trails', "
                "'flood', 'zoning'). Do not ask the user for clarification "
                "if the topic is obvious from their message."
            )
        limit = min(int(args.get("limit", 8)), 50)

        gallery_results, layer_results = await asyncio.gather(
            self._search_gallery(topic, limit),
            self._search_org_layers(
                topic, self.LAYER_TYPES + self.DATA_TYPES, limit
            ),
        )

        city = self.plugin_config.city_name
        gallery_url = self.plugin_config.gallery_url

        if not gallery_results and not layer_results:
            return (
                f"No {city} GIS content found for topic '{topic}'.\n\n"
                f"NEXT STEP: retry with a broader, simpler keyword. "
                f"Strip qualifiers like 'parcels', 'data', 'boundaries', "
                f"'zones', 'areas' -- e.g. 'park parcels' -> 'parks', "
                f"'flood zone boundaries' -> 'flood', 'school district "
                f"areas' -> 'schools'. Use the most distinctive single "
                f"word first.\n\n"
                f"If still no results after retry, browse the full "
                f"gallery: {gallery_url}"
            )

        text = f"## {city} GIS Content: '{topic}'\n\n"
        if gallery_results:
            text += (
                f"### Maps, Apps & Viewers "
                f"({len(gallery_results)} found)\n\n"
            )
            for item in gallery_results:
                text += self._format_summary(item)
        if layer_results:
            text += self._format_layer_section(layer_results)
        text += (
            "\n---\n"
            "**NEXT STEPS** (pick based on the user's question):\n"
            "- COUNT records ('how many?'): "
            "`query_data(item_id, limit=1)` -- read the TOTAL COUNT "
            "line in the response.\n"
            "- LIST records: "
            "`query_data(item_id, where=..., limit=N)`.\n"
            "- DESCRIBE an item: `get_item_details(item_id)`.\n"
            "- DISCOVER fields before filtering: "
            "`get_layer_schema(item_id)`.\n"
            "**Pick from the QUERYABLE section above** -- those are "
            "Feature/Map Services that work with `query_data`. Items "
            "in the OTHER section are viewers, web maps, and "
            "downloadable files (not directly queryable).\n"
            f"_Full gallery: {gallery_url}_"
        )
        return text

    def _format_layer_section(
        self, layer_results: List[Dict[str, Any]]
    ) -> str:
        """Render the spatial-layers block, split queryable vs other.

        Esri's relevance ranking already puts canonical Feature
        Services first within their type, but interleaving them with
        Web Maps in a single list lets weaker models latch onto a
        non-queryable item. The split makes the queryable choices
        visually unmistakable.
        """
        queryable, other = [], []
        for item in layer_results:
            if item.get("type") in self.QUERYABLE_TYPES:
                queryable.append(item)
            else:
                other.append(item)

        text = (
            f"### Spatial Layers & Data "
            f"({len(layer_results)} found)\n\n"
        )
        if queryable:
            text += (
                f"#### QUERYABLE -- Feature/Map Services "
                f"({len(queryable)})\n"
                f"_Use these directly with `query_data`, "
                f"`get_layer_schema`, `spatial_query_*`._\n"
            )
            if len(queryable) >= 2:
                text += (
                    "\n> **AMBIGUITY WARNING:** multiple queryable "
                    "layers match this topic. They may be maintained "
                    "by different agencies (e.g. municipal vs state "
                    "vs federal) or cover different subsets (e.g. "
                    "all trails vs nordic trails only). For 'how "
                    "many?' / 'list all' questions, do NOT silently "
                    "pick the first one -- either (a) query each "
                    "layer with `limit=1` and report a breakdown of "
                    "totals, or (b) ask the user which subset they "
                    "mean (e.g. 'municipal Parks & Rec', 'state-"
                    "managed', 'all combined'). The titles below "
                    "hint at scope (look for agency prefixes like "
                    "'ADNR', 'USFS', 'ParksRec', 'NSAA').\n\n"
                )
            else:
                text += "\n"
            for item in queryable:
                text += self._format_summary(item)
        if other:
            text += (
                f"#### OTHER -- Web Maps & Downloadable Data "
                f"({len(other)})\n"
                f"_Viewers and reference items. Not directly "
                f"queryable; use for context or to find the underlying "
                f"Feature Service via `get_item_details`._\n\n"
            )
            for item in other:
                text += self._format_summary(item)
        return text

    async def _browse_gallery(self, keyword: str, limit: int) -> str:
        """Browse or search the curated gallery."""
        results = await self._search_gallery(keyword, limit)
        city = self.plugin_config.city_name
        gallery_url = self.plugin_config.gallery_url

        if not results:
            suffix = f" matching {repr(keyword)}" if keyword else ""
            return f"No gallery items found{suffix}."

        header = (
            f"## {city} GIS Gallery -- '{keyword}' "
            f"({len(results)} items)\n\n"
            if keyword
            else f"## {city} GIS Gallery ({len(results)} items)\n\n"
        )
        text = header
        for item in results:
            text += self._format_summary(item)
        text += (
            "\n---\n"
            "**These are VIEWERS -- not directly queryable.** Web "
            "Maps, Dashboards, and Apps cannot be passed to "
            "`query_data` for record counts or filtered lists. If "
            "the user asked 'how many?' or 'list X', call "
            "`find_gis_content(topic=...)` to find the underlying "
            "Feature Service instead.\n"
            f"_Full gallery: {gallery_url}_"
        )
        return text

    async def _search_spatial_layers(
        self, query: str, layer_type: str, limit: int
    ) -> str:
        """Search raw spatial layers."""
        city = self.plugin_config.city_name
        if layer_type == "layers":
            item_types = self.LAYER_TYPES
        elif layer_type == "data":
            item_types = self.DATA_TYPES
        else:
            item_types = self.LAYER_TYPES + self.DATA_TYPES

        results = await self._search_org_layers(query, item_types, limit)
        if not results:
            return (
                f"No {city} spatial layers/data found matching "
                f"'{query}'.\n\n"
                f"NEXT STEP: retry with a broader keyword (strip "
                f"'parcels', 'data', 'boundaries' -- e.g. 'park "
                f"parcels' -> 'parks'), or call `find_gis_content` "
                f"to also search the curated public gallery."
            )

        text = (
            f"## {city} Spatial Layers: '{query}' "
            f"({len(results)} results)\n\n"
        )
        text += self._format_layer_section(results)
        text += (
            "\n---\n"
            "**NEXT STEPS:** `query_data(item_id, limit=1)` to count "
            "records (read the TOTAL COUNT line); "
            "`query_data(item_id, where=..., limit=N)` to list; "
            "`get_layer_schema(item_id)` to see field names; "
            "`get_item_details(item_id)` for the full description. "
            "Pick from the QUERYABLE section above for `query_data` "
            "calls.\n"
        )
        return text

    async def _get_layer_schema(self, args: Dict[str, Any]) -> str:
        """Fetch schema for a Feature/Map Service layer."""
        item_id = args.get("item_id", "").strip()
        service_url = args.get("service_url", "").strip()
        layer_index = int(args.get("layer_index", 0))
        keyword = args.get("keyword", "").strip().lower()
        item_title = service_url or item_id

        if item_id and not service_url:
            item_id = self._validate_item_id(item_id)
            item = await self.get_dataset(item_id)
            service_url = item.get("url", "")
            item_title = item.get("title", item_id)

        if not service_url:
            return "Error: provide either item_id or service_url."

        service_url = service_url.rstrip("/")
        if not re.search(r"/\d+$", service_url):
            service_url = f"{service_url}/{layer_index}"

        self._validate_service_url(service_url)
        resp = await self.client.get(service_url, params={"f": "json"})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                data["error"].get("message", str(data["error"]))
            )

        fields = data.get("fields", [])
        layer_name = data.get("name", data.get("title", "Unknown Layer"))
        geometry_type = data.get("geometryType", "N/A")
        max_records = data.get("maxRecordCount", "Unknown")

        # Prefer the layer name over a raw URL for the heading
        if not item_id and layer_name != "Unknown Layer":
            item_title = layer_name

        if keyword:
            fields = [
                f
                for f in fields
                if keyword in f.get("name", "").lower()
                or keyword in f.get("alias", "").lower()
            ]

        if not fields:
            return (
                f"No fields matching '{keyword}' in layer '{layer_name}'."
                if keyword
                else f"No fields found in layer '{layer_name}'."
            )

        text = f"## Schema: {item_title}\n"
        text += (
            f"**Layer:** {layer_name}  |  "
            f"**Geometry:** {geometry_type}  |  "
            f"**Max Records:** {max_records}\n"
        )
        text += f"**Service URL:** {service_url}\n\n"
        if keyword:
            text += f"_(Filtered to fields matching '{keyword}')_\n\n"
        text += f"### Fields ({len(fields)})\n\n"
        text += "| Field Name | Alias | Type |\n|---|---|---|\n"
        for f in fields:
            name = f.get("name", "")
            alias = f.get("alias", name)
            ftype = f.get("type", "").replace("esriFieldType", "")
            text += f"| `{name}` | {alias} | {ftype} |\n"

        domain_lines = []
        for f in fields:
            domain = f.get("domain")
            if domain and domain.get("type") == "codedValue":
                dname = domain.get("name", f.get("name", ""))
                codes = domain.get("codedValues", [])
                if codes:
                    vals = ", ".join(
                        f"{c['code']}={c['name']}" for c in codes[:15]
                    )
                    if len(codes) > 15:
                        vals += f" ... (+{len(codes) - 15} more)"
                    domain_lines.append(f"- **{dname}**: {vals}")

        if domain_lines:
            text += "\n### Coded Domains\n" + "\n".join(domain_lines)

        # Pick a real text-typed field from THIS layer for the
        # example, so the model sees a concrete, copy-pasteable call
        # instead of a generic placeholder. Falls back to "<field>"
        # if no string fields exist (rare for a public Feature
        # Service -- most have at least a name/title column).
        sample_text_field = next(
            (
                f.get("name")
                for f in fields
                if f.get("type") == "esriFieldTypeString"
                and f.get("name")
                and f.get("name").upper() != "OBJECTID"
            ),
            None,
        )
        sample_id_arg = (
            f"item_id='{item_id}'" if item_id else "item_id=<id>"
        )
        if sample_text_field:
            example_call = (
                f"`query_data({sample_id_arg}, "
                f"where=\"{sample_text_field} LIKE '%foo%'\", "
                f"limit=10)`"
            )
        else:
            example_call = (
                f"`query_data({sample_id_arg}, where=\"<Field>=<value>\", "
                f"limit=10)`"
            )

        text += (
            "\n\n---\n"
            "**NEXT STEPS:** use the field names above in `query_data` "
            "-- field names are CASE-SENSITIVE (use the exact `Field "
            "Name` column, not the alias). Quote string literals "
            "with single quotes. For text searches prefer `LIKE "
            "'%substring%'` over `=` (which requires the full exact "
            "value).\n\n"
            f"Example: {example_call}\n\n"
            "To just COUNT matches, set `limit=1` and read the "
            "TOTAL COUNT line in the response."
        )
        return text

    async def _search_layers_by_field(self, args: Dict[str, Any]) -> str:
        """Find services containing a specific field name/alias."""
        field_keyword = args.get("field_keyword", "").strip().lower()
        if not field_keyword:
            raise ValueError("field_keyword is required")
        service_keyword = args.get("service_keyword", "").strip()
        limit = min(int(args.get("limit", 10)), 20)

        type_filter = " OR ".join(f'type:"{t}"' for t in self.LAYER_TYPES)
        clauses = [
            f"orgid:{self.plugin_config.org_id}",
            f"({type_filter})",
        ]
        # Without a text filter, ArcGIS ranks the catalog by global popularity
        # and the top-`limit` pool skips less-trafficked services. Fall back to
        # the field_keyword so the sample is at least biased toward services
        # whose titles/descriptions mention the attribute of interest.
        effective_service_filter = service_keyword or field_keyword
        clauses.append(effective_service_filter)
        candidates = await self._run_search(" AND ".join(clauses), limit)

        if not candidates:
            return (
                f"No services found matching '{effective_service_filter}'."
            )

        # Bound concurrent ArcGIS calls. Without this, a 20-candidate search
        # can fire 20 service-root fetches plus 20*N layer-schema fetches in
        # parallel against the upstream portal -- a polite-burst that still
        # looks like a small DDoS to muniorg.maps.arcgis.com.
        inspect_sem = asyncio.Semaphore(5)

        async def check_service(
            item: Dict[str, Any],
        ) -> List[Dict[str, Any]]:
            url = (item.get("url") or "").rstrip("/")
            if not url:
                return []
            try:
                self._validate_service_url(url)
            except ValueError:
                return []
            async with inspect_sem:
                return await _inspect_service(item, url)

        async def _inspect_service(
            item: Dict[str, Any], url: str
        ) -> List[Dict[str, Any]]:
            try:
                # Fetch service root to discover all layers
                resp = await self.client.get(
                    url, params={"f": "json"}, timeout=10.0
                )
                root = resp.json()
                layer_list = root.get("layers", [])
                if not layer_list:
                    # Fallback: single-layer service, check /0
                    layer_list = [{"id": 0}]

                hits = []
                for layer_meta in layer_list:
                    layer_id = layer_meta.get("id", 0)
                    try:
                        lr = await self.client.get(
                            f"{url}/{layer_id}",
                            params={"f": "json"},
                            timeout=10.0,
                        )
                        data = lr.json()
                        matching = [
                            f
                            for f in data.get("fields", [])
                            if field_keyword in f.get("name", "").lower()
                            or field_keyword in f.get("alias", "").lower()
                        ]
                        if matching:
                            hits.append(
                                {
                                    "item": item,
                                    "layer_name": data.get("name", ""),
                                    "layer_index": layer_id,
                                    "matching_fields": matching,
                                }
                            )
                    except Exception:
                        continue
                return hits
            except Exception:
                return []

        results_raw = await asyncio.gather(
            *[check_service(item) for item in candidates]
        )
        matches = [m for hits in results_raw for m in hits]

        city = self.plugin_config.city_name

        if not matches:
            inspected = "\n".join(
                f"- {item.get('title', 'Untitled')}" for item in candidates
            )
            filter_note = (
                f"pre-filtered by '{service_keyword}'"
                if service_keyword
                else f"auto-filtered by field_keyword '{field_keyword}'"
            )
            return (
                f"None of the {len(candidates)} {city} services inspected "
                f"contain fields matching '{field_keyword}' ({filter_note}).\n\n"
                f"Inspected services:\n{inspected}\n\n"
                f"Try a different `service_keyword` to broaden the search."
            )

        text = (
            f"## {city} Layers with '{field_keyword}' Fields\n\n"
            f"Found {len(matches)} layer(s) with matching fields "
            f"(checked {len(candidates)} services):\n\n"
        )
        for m in matches:
            item = m["item"]
            title = item.get("title", "Untitled")
            item_id = item.get("id", "")
            layer_idx = m.get("layer_index", 0)
            layer_suffix = (
                f" (layer {layer_idx})" if layer_idx != 0 else ""
            )
            text += f"### {title}{layer_suffix}\n"
            text += (
                f"**Layer:** {m['layer_name']}  |  **ID:** `{item_id}`\n"
            )
            text += "**Matching fields:**\n"
            for f in m["matching_fields"]:
                name = f.get("name", "")
                alias = f.get("alias", "")
                ftype = f.get("type", "").replace("esriFieldType", "")
                label = f"`{name}`" + (
                    f" ({alias})" if alias != name else ""
                )
                text += f"- {label} -- {ftype}\n"
            text += (
                f"**Portal:** "
                f"{self._portal_home}/home/item.html?id={item_id}\n\n"
            )
        text += (
            "_Use `get_layer_schema` with an item_id to see the "
            "complete field list._"
        )
        return text

    # ── Static helpers ────────────────────────────────────────────────────

    def _validate_service_url(self, url: str) -> str:
        """Reject any URL whose host is not on the allowlist.

        Prevents SSRF and tenant-scope creep via user-supplied service
        URLs or item URLs returned from portal search results.

        For ``*.arcgis.com`` (ArcGIS Online), the URL must either match
        this org's portal host (e.g. ``muniorg.maps.arcgis.com``) or
        include the configured ``org_id`` as the first path segment
        (e.g. ``services.arcgis.com/<org_id>/...``,
        ``tiles7.arcgis.com/<org_id>/...``). This keeps the MCP from
        being used as an open proxy for arbitrary ArcGIS Online tenants.

        On-prem MOA hosts (``*.muni.org``) are accepted by suffix.
        """
        if not url:
            raise ValueError("service URL cannot be empty")
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"service URL must use http or https (got {parsed.scheme!r})"
            )
        host = (parsed.hostname or "").lower()
        if not host:
            raise ValueError("service URL must include a hostname")

        if any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in self.ONPREM_HOST_SUFFIXES
        ):
            return url

        if host == "arcgis.com" or host.endswith(".arcgis.com"):
            portal_host = self._portal_host
            if portal_host and host == portal_host:
                return url
            org_id = (self.plugin_config.org_id or "").lower() if self.plugin_config else ""
            if org_id and parsed.path.lower().startswith(f"/{org_id}/"):
                return url
            raise ValueError(
                f"service URL host {host!r} (path {parsed.path!r}) is not "
                f"scoped to org {org_id!r}; refusing to proxy other "
                f"ArcGIS Online tenants"
            )

        raise ValueError(
            f"service URL host {host!r} is not on the allowlist"
        )

    @property
    def _portal_host(self) -> str:
        """Hostname of the configured ArcGIS portal (lowercased)."""
        if not self.plugin_config or not self.plugin_config.portal_base_url:
            return ""
        return (urlparse(self.plugin_config.portal_base_url).hostname or "").lower()

    @classmethod
    def _validate_item_id(cls, item_id: str) -> str:
        """Require an ArcGIS item ID to be a 32-char hex string."""
        if not item_id or not cls.ITEM_ID_RE.match(item_id):
            raise ValueError(
                f"Invalid ArcGIS item id: {item_id!r} "
                f"(expected 32-character hex string)"
            )
        return item_id.lower()

    @staticmethod
    def _ensure_layer_url(service_url: str) -> str:
        """Append /0 if URL points at a FeatureServer/MapServer root."""
        stripped = service_url.rstrip("/")
        if re.search(r"/(FeatureServer|MapServer)$", stripped, re.IGNORECASE):
            return f"{stripped}/0"
        return stripped

    # Anchorage muni bbox in WGS84 (xmin, ymin, xmax, ymax). Generous
    # envelope around Cook Inlet to Eklutna -- used for the
    # coverage-gap devil's-advocate check, not for filtering. Exact
    # boundary fidelity isn't needed; the goal is to flag clearly
    # partial-coverage layers (e.g. a single neighborhood layer).
    ANCHORAGE_BBOX_WGS84 = (-150.5, 60.5, -148.5, 61.6)

    @staticmethod
    def _webmerc_to_wgs84(x: float, y: float) -> Tuple[float, float]:
        # Inline Web Mercator -> WGS84 to avoid a pyproj dependency for
        # one coordinate conversion. Earth radius per EPSG:3857 spec.
        import math
        lon = x / 6378137.0 * 180.0 / math.pi
        lat = (
            math.atan(math.exp(y / 6378137.0)) * 2.0 - math.pi / 2.0
        ) * 180.0 / math.pi
        return lon, lat

    @classmethod
    def _anchorage_coverage_pct(cls, extent: Any) -> Optional[float]:
        # Returns (layer intersect muni) / muni-area, in [0, ~1+]. >1 if the
        # layer is larger than the muni (statewide data). Used to fire
        # the LIMITED COVERAGE caveat for layers covering <50% of the
        # muni. Returns None when we can't honestly compute coverage
        # (unhandled SR, malformed extent) so the caveat is suppressed
        # rather than guessed.
        if not isinstance(extent, dict):
            return None
        coords = [extent.get(k) for k in ("xmin", "ymin", "xmax", "ymax")]
        if any(not isinstance(v, (int, float)) for v in coords):
            return None
        xmin, ymin, xmax, ymax = coords
        # Degenerate bbox (single point or zero-width/height) carries no
        # useful coverage signal -- a hospital layer with one point in
        # downtown isn't "non-overlapping Anchorage", it just has no
        # area. Return None so the caveat stays silent.
        if xmin == xmax or ymin == ymax:
            return None
        sr = extent.get("spatialReference") or {}
        wkid = sr.get("wkid") or sr.get("latestWkid")
        if wkid in (4326, 4269):
            pass
        elif wkid in (102100, 3857, 102113):
            xmin, ymin = cls._webmerc_to_wgs84(xmin, ymin)
            xmax, ymax = cls._webmerc_to_wgs84(xmax, ymax)
        else:
            return None
        muni = cls.ANCHORAGE_BBOX_WGS84
        ix = max(xmin, muni[0])
        iy = max(ymin, muni[1])
        ax = min(xmax, muni[2])
        ay = min(ymax, muni[3])
        if ix >= ax or iy >= ay:
            return 0.0
        intersection = (ax - ix) * (ay - iy)
        muni_area = (muni[2] - muni[0]) * (muni[3] - muni[1])
        return intersection / muni_area

    @staticmethod
    def _validate_lonlat(lon: Any, lat: Any) -> tuple[float, float]:
        """Validate WGS84 coordinates. Note: lon first, then lat."""
        try:
            lon_f = float(lon)
            lat_f = float(lat)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"lon/lat must be numeric (got lon={lon!r}, lat={lat!r})"
            ) from e
        if not (-180.0 <= lon_f <= 180.0):
            raise ValueError(
                f"lon out of range [-180, 180]: {lon_f}"
            )
        if not (-90.0 <= lat_f <= 90.0):
            raise ValueError(
                f"lat out of range [-90, 90]: {lat_f}"
            )
        return lon_f, lat_f

    async def _get_record_count(
        self, service_url: str, where: str
    ) -> Optional[int]:
        """Fetch total record count for a query (best-effort)."""
        try:
            self._validate_service_url(service_url)
        except ValueError:
            return None
        query_url = f"{service_url}/query"
        try:
            resp = await self.client.get(
                query_url,
                params={
                    "where": where,
                    "returnCountOnly": "true",
                    "f": "json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            count = data.get("count")
            return int(count) if count is not None else None
        except Exception:
            return None

    async def _safe_layer_meta(self, item_id: str) -> Dict[str, Any]:
        # Best-effort lookup of the layer quick-meta (date fields,
        # coded domains, geometry type, name field) for an item.
        # Used by paths (spatial_*) that don't otherwise need the layer
        # URL upstream -- resolve via get_dataset, then quick_meta. Any
        # failure returns {} so callers fall back to raw rendering
        # rather than blocking the user's spatial result.
        try:
            item = await self.get_dataset(item_id)
            service_url = self._ensure_layer_url(item.get("url", ""))
            if not service_url:
                return {}
            return await self._get_layer_quick_meta(service_url) or {}
        except Exception:
            return {}

    async def _get_layer_quick_meta(
        self, service_url: str
    ) -> Dict[str, Any]:
        """Fetch a small bundle of layer metadata used by the query
        formatter: date field names (for epoch->ISO conversion),
        coded-value domains (for decoding raw codes in output),
        and geometry type (for the polyline-grain warning on counts).
        Best-effort -- returns an empty dict on any error so callers
        can degrade gracefully.

        Returns ``{date_fields: set|None, coded_domains: dict|None,
        geometry_type: str|None, name_field: str|None}``.
        ``coded_domains`` maps field name -> {code: label} for fields
        whose schema declares an ``esriFieldTypeCodedValue`` domain,
        letting the formatter render ``ZONE: R1A
        (Single-Family Residential)`` instead of forcing the model to
        guess what ``R1A`` means. ``name_field`` is the first
        natural-ID-style field present, used to suggest a follow-up
        get_distinct_values call when warning about polyline grain.
        """
        try:
            self._validate_service_url(service_url)
        except ValueError:
            return {}
        try:
            resp = await self.client.get(
                service_url, params={"f": "json"}
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return {}
        fields = data.get("fields") or []
        date_fields = {
            f["name"]
            for f in fields
            if f.get("type") == "esriFieldTypeDate"
        } or None
        coded_domains: Dict[str, Dict[Any, str]] = {}
        for f in fields:
            domain = f.get("domain") or {}
            if domain.get("type") != "codedValue":
                continue
            fname = f.get("name")
            coded_values = domain.get("codedValues") or []
            mapping = {
                cv.get("code"): cv.get("name")
                for cv in coded_values
                if cv.get("code") is not None and cv.get("name")
            }
            if fname and mapping:
                coded_domains[fname] = mapping
        field_names = {f.get("name") for f in fields if f.get("name")}
        name_field = next(
            (
                f
                for f in self.NATURAL_ID_FIELD_PRIORITY
                if f in field_names
            ),
            None,
        )
        # Devil's-advocate signals -- pre-compute and stash so the
        # formatter can fire the staleness and coverage caveats without
        # a second round-trip. Both are best-effort: missing/malformed
        # values just suppress the corresponding caveat.
        editing = data.get("editingInfo") or {}
        last_edit_date = (
            editing.get("dataLastEditDate")
            or editing.get("lastEditDate")
        )
        if not isinstance(last_edit_date, (int, float)):
            last_edit_date = None
        coverage_pct = self._anchorage_coverage_pct(data.get("extent"))

        return {
            "date_fields": date_fields,
            "coded_domains": coded_domains or None,
            "geometry_type": data.get("geometryType"),
            "name_field": name_field,
            "field_names": field_names or None,
            "last_edit_date": last_edit_date,
            "coverage_pct": coverage_pct,
        }

    # ── Aggregation helpers ───────────────────────────────────────────────

    # Cache TTL for aggregation layers (councils, districts, etc.). Boundary
    # layers change rarely; paying 24h of staleness buys a huge hit-rate win
    # on repeat analyses.
    AGG_CACHE_TTL_SECONDS = 86400

    # Cap on LRU entries. Each entry can hold up to AGG_SOURCE_LIMIT polygons;
    # this bounds worst-case memory at roughly 32 * ~5 MB = ~160 MB and
    # defangs WHERE-clause-variant cache-busting DoS attempts.
    AGG_CACHE_MAX_ENTRIES = 32

    # Safety cap on source features pulled for a single aggregation call.
    # A council-by-council rollup of a city-wide dataset is typically a few
    # hundred to a few thousand features; beyond this the analysis is
    # probably better served by a server-side stats endpoint. 2000 is the
    # tightest value that still covers all real Anchorage rollups observed
    # in CloudWatch -- raise only with evidence of legitimate truncation.
    AGG_SOURCE_LIMIT = 2000

    # ArcGIS maxRecordCount is usually 1000 or 2000. We page through with
    # resultOffset at this step until AGG_SOURCE_LIMIT is reached.
    AGG_PAGE_SIZE = 1000

    # Caps for find_features_spanning_classifications. The source cap is
    # higher than AGG_SOURCE_LIMIT because the spanning analysis only
    # returns the qualifying subset (typically << source size); the
    # classification cap is higher than expected zoning/floodplain/council
    # polygon counts so a real layer never silently truncates.
    SPANNING_SOURCE_LIMIT = 5000
    SPANNING_CLASSIFICATION_LIMIT = 1000

    # Concurrency cap for per-classification spatial queries. The portal
    # tolerates polite bursts; a hard cap keeps the spanning tool from
    # looking like a small DDoS to muniorg.maps.arcgis.com when a
    # classification layer has hundreds of polygons.
    SPANNING_QUERY_CONCURRENCY = 10

    @staticmethod
    def _ring_contains_point(
        ring: List[List[float]], point: Tuple[float, float]
    ) -> bool:
        """Even-odd ray-cast point-in-ring test (2D, lon/lat).

        Expects ring as a list of [lon, lat] pairs (GeoJSON style, first
        and last point equal). Returns True if the point is strictly
        inside the ring; boundary behavior is not guaranteed (doesn't
        matter for aggregation -- a point exactly on a council boundary
        falls into exactly one bucket under first_match).
        """
        x, y = point
        inside = False
        n = len(ring)
        if n < 3:
            return False
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            # Does the horizontal ray at y cross edge (i, j)?
            intersect = ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi
            )
            if intersect:
                inside = not inside
            j = i
        return inside

    @classmethod
    def _polygon_contains_point(
        cls,
        polygon: List[List[List[float]]],
        point: Tuple[float, float],
    ) -> bool:
        """Point-in-polygon for a GeoJSON Polygon coordinates array.

        Rings after the first are treated as holes: a point inside a hole
        is not inside the polygon.
        """
        if not polygon:
            return False
        if not cls._ring_contains_point(polygon[0], point):
            return False
        for hole in polygon[1:]:
            if cls._ring_contains_point(hole, point):
                return False
        return True

    @classmethod
    def _multipolygon_contains_point(
        cls,
        multipolygon: List[List[List[List[float]]]],
        point: Tuple[float, float],
    ) -> bool:
        return any(cls._polygon_contains_point(p, point) for p in multipolygon)

    @classmethod
    def _geometry_contains_point(
        cls, geometry: Dict[str, Any], point: Tuple[float, float]
    ) -> bool:
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates")
        if not coords:
            return False
        if gtype == "Polygon":
            return cls._polygon_contains_point(coords, point)
        if gtype == "MultiPolygon":
            return cls._multipolygon_contains_point(coords, point)
        return False

    # Meters per degree of latitude (spherical mean Earth radius). Degrees
    # of longitude are scaled by cos(latitude) so the planar approximation
    # stays metric at Anchorage's ~61 degN, where a degree of longitude is
    # only ~half a degree of latitude in ground distance.
    _M_PER_DEG_LAT = 111195.0

    @classmethod
    def _point_segment_distance_m(
        cls,
        point: Tuple[float, float],
        a: List[float],
        b: List[float],
        lat_scale: float,
    ) -> float:
        """Shortest distance (meters) from `point` to segment a-b.

        Works in a local equirectangular frame centered on `point`:
        longitudes are scaled by `lat_scale` = cos(lat) so x/y are both in
        meters. Accurate to well under a percent over the few-km spans this
        is used for.
        """
        px, py = point
        ax = (a[0] - px) * cls._M_PER_DEG_LAT * lat_scale
        ay = (a[1] - py) * cls._M_PER_DEG_LAT
        bx = (b[0] - px) * cls._M_PER_DEG_LAT * lat_scale
        by = (b[1] - py) * cls._M_PER_DEG_LAT
        dx = bx - ax
        dy = by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq <= 0.0:
            return (ax * ax + ay * ay) ** 0.5
        # Project the origin (the point) onto the segment, clamped to [0, 1].
        t = -(ax * dx + ay * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
        cx = ax + t * dx
        cy = ay + t * dy
        return (cx * cx + cy * cy) ** 0.5

    @classmethod
    def _point_to_geometry_distance_m(
        cls, geometry: Dict[str, Any], point: Tuple[float, float]
    ) -> float:
        """Shortest distance (meters) from a point to a polygon boundary.

        Returns 0.0 when the point is inside the polygon (holes respected),
        otherwise the minimum distance to any ring edge. Used to test
        proximity ("within N of this polygon") in aggregate_by_polygon.
        """
        if cls._geometry_contains_point(geometry, point):
            return 0.0
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates") or []
        rings: List[List[List[float]]] = []
        if gtype == "Polygon":
            rings = list(coords)
        elif gtype == "MultiPolygon":
            for poly in coords:
                rings.extend(poly)
        else:
            return float("inf")
        lat_scale = math.cos(math.radians(point[1]))
        best = float("inf")
        for ring in rings:
            for i in range(len(ring) - 1):
                d = cls._point_segment_distance_m(
                    point, ring[i], ring[i + 1], lat_scale
                )
                if d < best:
                    best = d
                    if best == 0.0:
                        return 0.0
        return best

    @staticmethod
    def _ring_area(ring: List[List[float]]) -> float:
        """Shoelace area (signed). Positive for CCW rings."""
        area = 0.0
        n = len(ring)
        if n < 3:
            return 0.0
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            area += x1 * y2 - x2 * y1
        return area / 2.0

    @classmethod
    def _polygon_area(cls, polygon: List[List[List[float]]]) -> float:
        if not polygon:
            return 0.0
        a = abs(cls._ring_area(polygon[0]))
        for hole in polygon[1:]:
            a -= abs(cls._ring_area(hole))
        return a

    @classmethod
    def _geometry_area(cls, geometry: Dict[str, Any]) -> float:
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates") or []
        if gtype == "Polygon":
            return cls._polygon_area(coords)
        if gtype == "MultiPolygon":
            return sum(cls._polygon_area(p) for p in coords)
        return 0.0

    @staticmethod
    def _ring_centroid(ring: List[List[float]]) -> Tuple[float, float]:
        """Area-weighted centroid of a ring (first==last tolerated).

        Falls back to arithmetic mean of vertices for degenerate rings
        (colinear points, zero area).
        """
        n = len(ring)
        if n == 0:
            return (0.0, 0.0)
        # Drop duplicated closing vertex if present
        if n > 1 and ring[0] == ring[-1]:
            pts = ring[:-1]
        else:
            pts = ring
        m = len(pts)
        if m < 3:
            sx = sum(p[0] for p in pts) / m
            sy = sum(p[1] for p in pts) / m
            return (sx, sy)
        cx = 0.0
        cy = 0.0
        a = 0.0
        for i in range(m):
            x1, y1 = pts[i][0], pts[i][1]
            x2, y2 = pts[(i + 1) % m][0], pts[(i + 1) % m][1]
            cross = x1 * y2 - x2 * y1
            a += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        a *= 0.5
        if abs(a) < 1e-18:
            sx = sum(p[0] for p in pts) / m
            sy = sum(p[1] for p in pts) / m
            return (sx, sy)
        return (cx / (6.0 * a), cy / (6.0 * a))

    @classmethod
    def _geometry_centroid(
        cls, geometry: Dict[str, Any]
    ) -> Optional[Tuple[float, float]]:
        """Centroid of a GeoJSON geometry (Polygon/MultiPolygon/Point).

        For MultiPolygon, returns the area-weighted centroid of the
        constituent polygon centroids.
        """
        gtype = (geometry or {}).get("type", "")
        coords = (geometry or {}).get("coordinates")
        if coords is None:
            return None
        if gtype == "Point":
            return (float(coords[0]), float(coords[1]))
        if gtype == "Polygon":
            if not coords:
                return None
            return cls._ring_centroid(coords[0])
        if gtype == "MultiPolygon":
            if not coords:
                return None
            sum_x = 0.0
            sum_y = 0.0
            sum_a = 0.0
            for poly in coords:
                if not poly:
                    continue
                area = cls._polygon_area(poly)
                cx, cy = cls._ring_centroid(poly[0])
                sum_x += cx * area
                sum_y += cy * area
                sum_a += area
            if sum_a <= 0:
                # Fall back to centroid of largest polygon
                largest = max(coords, key=lambda p: cls._polygon_area(p))
                return cls._ring_centroid(largest[0]) if largest else None
            return (sum_x / sum_a, sum_y / sum_a)
        return None

    @classmethod
    def _geometry_representative_point(
        cls, geometry: Dict[str, Any]
    ) -> Optional[Tuple[float, float]]:
        """Return a point guaranteed to be inside the geometry (best-effort).

        Uses the centroid when it's inside the geometry, otherwise sweeps
        a horizontal line at the centroid's y through the polygon and
        returns the midpoint of the widest interior segment. Final fallback
        is the first outer-ring vertex.
        """
        gtype = (geometry or {}).get("type", "")
        if gtype == "Point":
            coords = geometry.get("coordinates") or []
            return (float(coords[0]), float(coords[1])) if coords else None

        centroid = cls._geometry_centroid(geometry)
        if centroid is None:
            return None
        if cls._geometry_contains_point(geometry, centroid):
            return centroid

        # Horizontal sweep at y = centroid.y to find an interior segment.
        y = centroid[1]
        rings: List[List[List[float]]] = []
        coords = geometry.get("coordinates") or []
        if gtype == "Polygon":
            rings = list(coords)
        elif gtype == "MultiPolygon":
            for poly in coords:
                rings.extend(poly)

        xs: List[float] = []
        for ring in rings:
            n = len(ring)
            if n < 2:
                continue
            for i in range(n):
                x1, y1 = ring[i][0], ring[i][1]
                x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
                if (y1 > y) == (y2 > y):
                    continue
                dy = y2 - y1
                if dy == 0:
                    continue
                t = (y - y1) / dy
                xs.append(x1 + t * (x2 - x1))
        xs.sort()
        # Take the widest interior span (odd pairs are inside under even-odd).
        best_mid = None
        best_width = -1.0
        for i in range(0, len(xs) - 1, 2):
            mid = (xs[i] + xs[i + 1]) / 2.0
            width = xs[i + 1] - xs[i]
            if width > best_width and cls._geometry_contains_point(
                geometry, (mid, y)
            ):
                best_width = width
                best_mid = (mid, y)
        if best_mid is not None:
            return best_mid

        # Last resort: first outer-ring vertex.
        if gtype == "Polygon" and coords and coords[0]:
            v = coords[0][0]
            return (float(v[0]), float(v[1]))
        if gtype == "MultiPolygon" and coords and coords[0] and coords[0][0]:
            v = coords[0][0][0]
            return (float(v[0]), float(v[1]))
        return centroid

    # ── Polyline reduction helpers ────────────────────────────────────────
    # Coordinates arrive in WGS84 (outSR=4326) so segment lengths are in
    # degrees. That's fine for finding a midpoint -- the result is exact in
    # Euclidean degree-space and still lies on the line. Anchorage spans ~3 deg
    # at lat 61 degN where 1 deg lon ~ 0.5 x 111 km, so the along-line position is
    # geodesically biased ~2x longward, but that bias is shared by the line
    # itself (same projection) so the chosen point lands on the right segment.
    @staticmethod
    def _segment_length(p1: List[float], p2: List[float]) -> float:
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        return (dx * dx + dy * dy) ** 0.5

    @classmethod
    def _polyline_total_length(cls, coords: List[List[float]]) -> float:
        n = len(coords)
        if n < 2:
            return 0.0
        return sum(
            cls._segment_length(coords[i], coords[i + 1])
            for i in range(n - 1)
        )

    @classmethod
    def _polyline_point_at_length(
        cls, coords: List[List[float]], target: float
    ) -> Optional[Tuple[float, float]]:
        """Walk the polyline and return the point at `target` arc length."""
        n = len(coords)
        if n == 0:
            return None
        if n == 1 or target <= 0:
            return (float(coords[0][0]), float(coords[0][1]))
        accum = 0.0
        for i in range(n - 1):
            seg = cls._segment_length(coords[i], coords[i + 1])
            if accum + seg >= target:
                t = (target - accum) / seg if seg > 0 else 0.0
                x = coords[i][0] + t * (coords[i + 1][0] - coords[i][0])
                y = coords[i][1] + t * (coords[i + 1][1] - coords[i][1])
                return (float(x), float(y))
            accum += seg
        return (float(coords[-1][0]), float(coords[-1][1]))

    @classmethod
    def _polyline_midpoint(
        cls, coords: List[List[float]]
    ) -> Optional[Tuple[float, float]]:
        """Midpoint along a LineString by arc length (always lies on the line)."""
        total = cls._polyline_total_length(coords)
        return cls._polyline_point_at_length(coords, total / 2.0)

    @classmethod
    def _polyline_centroid(
        cls, coords: List[List[float]]
    ) -> Optional[Tuple[float, float]]:
        """Length-weighted centroid of a LineString.

        For a curved or U-shaped line the result can fall off the line.
        Use _polyline_midpoint when the point must lie on the line itself.
        """
        n = len(coords)
        if n == 0:
            return None
        if n == 1:
            return (float(coords[0][0]), float(coords[0][1]))
        sum_x = 0.0
        sum_y = 0.0
        sum_len = 0.0
        for i in range(n - 1):
            seg = cls._segment_length(coords[i], coords[i + 1])
            mid_x = (coords[i][0] + coords[i + 1][0]) / 2.0
            mid_y = (coords[i][1] + coords[i + 1][1]) / 2.0
            sum_x += mid_x * seg
            sum_y += mid_y * seg
            sum_len += seg
        if sum_len == 0:
            sx = sum(p[0] for p in coords) / n
            sy = sum(p[1] for p in coords) / n
            return (float(sx), float(sy))
        return (sum_x / sum_len, sum_y / sum_len)

    @classmethod
    def _multilinestring_midpoint(
        cls, lines: List[List[List[float]]]
    ) -> Optional[Tuple[float, float]]:
        """Midpoint along the concatenated arc length of all sub-lines."""
        if not lines:
            return None
        sub_lengths = [cls._polyline_total_length(line) for line in lines]
        total = sum(sub_lengths)
        if total == 0:
            for line in lines:
                if line:
                    return (float(line[0][0]), float(line[0][1]))
            return None
        target = total / 2.0
        accum = 0.0
        for line, seg_total in zip(lines, sub_lengths):
            if accum + seg_total >= target:
                return cls._polyline_point_at_length(line, target - accum)
            accum += seg_total
        for line in reversed(lines):
            if line:
                return (float(line[-1][0]), float(line[-1][1]))
        return None

    @classmethod
    def _multilinestring_centroid(
        cls, lines: List[List[List[float]]]
    ) -> Optional[Tuple[float, float]]:
        """Length-weighted centroid of a MultiLineString."""
        if not lines:
            return None
        sum_x = 0.0
        sum_y = 0.0
        sum_len = 0.0
        for line in lines:
            seg_total = cls._polyline_total_length(line)
            if seg_total == 0:
                continue
            c = cls._polyline_centroid(line)
            if c is None:
                continue
            sum_x += c[0] * seg_total
            sum_y += c[1] * seg_total
            sum_len += seg_total
        if sum_len == 0:
            for line in lines:
                if line:
                    return (float(line[0][0]), float(line[0][1]))
            return None
        return (sum_x / sum_len, sum_y / sum_len)

    @classmethod
    def _feature_to_point(
        cls, geometry: Dict[str, Any], centroid_mode: str
    ) -> Optional[Tuple[float, float]]:
        """Reduce a feature's geometry to a single (lon, lat) point.

        Handles Point/MultiPoint, LineString/MultiLineString (e.g. road
        centerlines, trails, transit routes), and Polygon/MultiPolygon.
        For lines, `centroid_mode` has line-specific meaning:
          - 'centroid' = length-weighted centroid (cheap; can fall off the line)
          - 'representative_point' / 'auto' = midpoint along arc length
            (always lies on the line -- the right default for "which polygon
            does this road segment belong to" bucketing).
        """
        if not geometry:
            return None
        gtype = geometry.get("type", "")
        if gtype == "Point":
            coords = geometry.get("coordinates") or []
            return (float(coords[0]), float(coords[1])) if coords else None
        if gtype == "MultiPoint":
            coords = geometry.get("coordinates") or []
            return (float(coords[0][0]), float(coords[0][1])) if coords else None
        if gtype == "LineString":
            coords = geometry.get("coordinates") or []
            if not coords:
                return None
            if centroid_mode == "centroid":
                return cls._polyline_centroid(coords)
            return cls._polyline_midpoint(coords)
        if gtype == "MultiLineString":
            lines = geometry.get("coordinates") or []
            if not lines:
                return None
            if centroid_mode == "centroid":
                return cls._multilinestring_centroid(lines)
            return cls._multilinestring_midpoint(lines)
        if centroid_mode == "centroid":
            return cls._geometry_centroid(geometry)
        if centroid_mode == "representative_point":
            return cls._geometry_representative_point(geometry)
        # auto: centroid if inside, else representative_point
        centroid = cls._geometry_centroid(geometry)
        if centroid is not None and cls._geometry_contains_point(
            geometry, centroid
        ):
            return centroid
        return cls._geometry_representative_point(geometry)

    async def _resolve_layer_url(self, item_id: str) -> str:
        """Resolve an item ID to a validated /FeatureServer/N query URL."""
        item_id = self._validate_item_id(item_id)
        item = await self.get_dataset(item_id)
        url = item.get("url", "")
        item_type = item.get("type", "")
        if not url:
            raise ValueError(
                f"Item {item_id} has no queryable service URL"
            )
        if item_type and item_type not in self.QUERYABLE_TYPES:
            raise ValueError(
                f"Item type '{item_type}' is not queryable."
            )
        url = self._ensure_layer_url(url)
        self._validate_service_url(url)
        return url

    async def _fetch_layer_meta(self, layer_url: str) -> Dict[str, Any]:
        resp = await self.client.get(layer_url, params={"f": "json"})
        resp.raise_for_status()
        meta = resp.json()
        if "error" in meta:
            raise RuntimeError(
                meta["error"].get("message", str(meta["error"]))
            )
        return meta

    async def _fetch_aggregation_polygons(
        self,
        aggregation_item_id: str,
        group_by_field: str,
        agg_where: str,
    ) -> List[Dict[str, Any]]:
        """Fetch polygons from an aggregation layer with their group value.

        Returns [{'group': value, 'geometry': {..GeoJSON..}, 'objectid': N}].
        Cached by (item_id, group_by_field, agg_where) for AGG_CACHE_TTL_SECONDS.
        """
        validated_where = WhereValidator.validate(agg_where or "1=1")
        cache_key = (
            self._validate_item_id(aggregation_item_id),
            group_by_field,
            validated_where,
        )
        now = time.time()
        cached = self._agg_layer_cache.get(cache_key)
        if cached and cached[0] > now:
            self._agg_layer_cache.move_to_end(cache_key)
            return cached[1]
        if cached:
            # Expired -- drop and refetch.
            del self._agg_layer_cache[cache_key]

        layer_url = await self._resolve_layer_url(aggregation_item_id)
        meta = await self._fetch_layer_meta(layer_url)
        geom_type = meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"aggregation_item_id must point at a polygon layer "
                f"(got geometryType={geom_type or 'unknown'!r})"
            )
        field_names = {f.get("name") for f in meta.get("fields", [])}
        if group_by_field not in field_names:
            raise ValueError(
                f"group_by_field {group_by_field!r} is not a field on "
                f"the aggregation layer. Available fields: "
                f"{sorted(field_names)[:12]}..."
            )

        polygons = await self._paged_geojson_fetch(
            layer_url,
            where=validated_where,
            out_fields=group_by_field,
            limit=self.AGG_SOURCE_LIMIT,
        )
        result = [
            {
                "group": (f.get("properties") or {}).get(group_by_field),
                "geometry": f.get("geometry"),
            }
            for f in polygons
            if f.get("geometry")
        ]
        self._agg_layer_cache[cache_key] = (
            now + self.AGG_CACHE_TTL_SECONDS,
            result,
        )
        # LRU eviction to bound memory under cache-busting inputs.
        while len(self._agg_layer_cache) > self.AGG_CACHE_MAX_ENTRIES:
            self._agg_layer_cache.popitem(last=False)
        return result

    async def _paged_geojson_fetch(
        self,
        layer_url: str,
        where: str,
        out_fields: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Page through a layer and return raw GeoJSON features (geom + props)."""
        query_url = f"{layer_url}/query"
        out_fields = OutFieldsValidator.validate(out_fields or "*")
        features: List[Dict[str, Any]] = []
        offset = 0
        page_size = self.AGG_PAGE_SIZE
        while len(features) < limit:
            params = {
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "true",
                "outSR": "4326",
                "f": "geojson",
                "resultRecordCount": str(min(page_size, limit - len(features))),
                "resultOffset": str(offset),
                "maxAllowableOffset": str(self.GEOMETRY_SIMPLIFY_OFFSET_DEG),
            }
            resp = await self.client.get(query_url, params=params)
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(
                    data["error"].get("message", str(data["error"]))
                )
            page = data.get("features") or []
            if not page:
                break
            features.extend(page)
            offset += len(page)
            # If we got less than requested, we're done.
            if len(page) < page_size:
                break
        return features

    async def _aggregate_by_polygon(self, args: Dict[str, Any]) -> str:
        source_item_id = self._validate_item_id(
            (args.get("source_item_id") or "").strip()
        )
        aggregation_item_id = self._validate_item_id(
            (args.get("aggregation_item_id") or "").strip()
        )
        group_by_field = (args.get("group_by_field") or "").strip()
        if not group_by_field:
            raise ValueError("group_by_field is required")
        raw_sum_fields = args.get("sum_fields") or []
        if isinstance(raw_sum_fields, str):
            raw_sum_fields = [
                s.strip() for s in raw_sum_fields.split(",") if s.strip()
            ]
        sum_fields: List[str] = list(raw_sum_fields)
        include_count = bool(args.get("count", True))
        # Validate both WHERE clauses up front so malformed/injection
        # attempts are rejected before any upstream I/O (otherwise a bad
        # source_where would still pay the aggregation-layer fetch cost).
        validated_source_where = WhereValidator.validate(
            args.get("source_where") or "1=1"
        )
        agg_where = WhereValidator.validate(args.get("agg_where") or "1=1")
        centroid_mode = (args.get("centroid_mode") or "auto").lower()
        if centroid_mode not in ("auto", "centroid", "representative_point"):
            raise ValueError(
                "centroid_mode must be one of: auto, centroid, "
                "representative_point"
            )
        overlap_policy = (args.get("overlap_policy") or "first_match").lower()
        if overlap_policy not in ("first_match", "all_matches", "largest"):
            raise ValueError(
                "overlap_policy must be one of: first_match, "
                "all_matches, largest"
            )
        # Optional proximity buffer: a source feature is bucketed into an
        # aggregation polygon when its point falls inside OR within
        # buffer_distance of that polygon. Answers "how many X within N of
        # each Y" (e.g. population points within half a mile of each park).
        raw_buffer = args.get("buffer_distance")
        buffer_m = 0.0
        buffer_units_canonical: Optional[str] = None
        if raw_buffer is not None and str(raw_buffer) != "":
            try:
                buffer_val = float(raw_buffer)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"buffer_distance must be a number "
                    f"(got {raw_buffer!r})"
                ) from exc
            if buffer_val < 0:
                raise ValueError(
                    f"buffer_distance must be >= 0 (got {buffer_val})"
                )
            if buffer_val > 0:
                buffer_units_canonical = self._normalize_linear_unit(
                    args.get("buffer_units")
                )
                buffer_m = (
                    buffer_val
                    * self._LINEAR_UNIT_TO_METERS[buffer_units_canonical]
                )
        max_source = min(
            int(args.get("max_source_features", self.AGG_SOURCE_LIMIT)),
            self.AGG_SOURCE_LIMIT,
        )

        # Fetch aggregation polygons first so we can validate the group field
        # name before paying for a source-layer fetch.
        agg_polygons = await self._fetch_aggregation_polygons(
            aggregation_item_id, group_by_field, agg_where
        )
        if not agg_polygons:
            raise ValueError(
                f"agg_where {agg_where!r} matched no polygons on "
                f"the aggregation layer"
            )

        # Fetch source features. Validate sum_fields exist.
        source_url = await self._resolve_layer_url(source_item_id)
        source_meta = await self._fetch_layer_meta(source_url)
        source_fields = {f.get("name") for f in source_meta.get("fields", [])}
        numeric_types = {
            "esriFieldTypeInteger",
            "esriFieldTypeSmallInteger",
            "esriFieldTypeDouble",
            "esriFieldTypeSingle",
            "esriFieldTypeOID",
        }
        numeric_fields = {
            f.get("name")
            for f in source_meta.get("fields", [])
            if f.get("type") in numeric_types
        }
        for f in sum_fields:
            if f not in source_fields:
                raise ValueError(
                    f"sum_fields entry {f!r} is not a field on the "
                    f"source layer"
                )
            if f not in numeric_fields:
                raise ValueError(
                    f"sum_fields entry {f!r} is not a numeric field "
                    f"(cannot be summed)"
                )
        source_geom_type = source_meta.get("geometryType", "")

        # Request only the fields we need + geometry.
        out_fields = ",".join(sum_fields) if sum_fields else "OBJECTID"
        source_features = await self._paged_geojson_fetch(
            source_url,
            where=validated_source_where,
            out_fields=out_fields,
            limit=max_source,
        )

        # Reduce each source feature to a point.
        source_points: List[Tuple[Tuple[float, float], Dict[str, Any]]] = []
        for feat in source_features:
            props = feat.get("properties") or {}
            geom = feat.get("geometry") or {}
            point = self._feature_to_point(geom, centroid_mode)
            if point is None:
                continue
            source_points.append((point, props))

        # Pre-compute polygon areas for 'largest' policy.
        if overlap_policy == "largest":
            for p in agg_polygons:
                p["_area"] = self._geometry_area(p["geometry"])

        buckets: Dict[Any, Dict[str, Any]] = defaultdict(
            lambda: {"count": 0, **{f: 0.0 for f in sum_fields}}
        )
        unmatched_count = 0
        for point, props in source_points:
            if buffer_m > 0:
                matches = [
                    p for p in agg_polygons
                    if self._point_to_geometry_distance_m(
                        p["geometry"], point
                    ) <= buffer_m
                ]
            else:
                matches = [
                    p for p in agg_polygons
                    if self._geometry_contains_point(p["geometry"], point)
                ]
            if not matches:
                unmatched_count += 1
                continue
            if overlap_policy == "first_match":
                matches = matches[:1]
            elif overlap_policy == "largest":
                matches = [max(matches, key=lambda p: p.get("_area", 0.0))]
            for p in matches:
                b = buckets[p["group"]]
                b["count"] += 1
                for fld in sum_fields:
                    v = props.get(fld)
                    if v is None:
                        continue
                    try:
                        b[fld] = (b[fld] or 0) + float(v)
                    except (TypeError, ValueError):
                        continue

        # Format: sorted by count desc for readability.
        bucket_list = sorted(
            buckets.items(),
            key=lambda kv: kv[1]["count"],
            reverse=True,
        )

        city = self.plugin_config.city_name
        buffer_label = (
            f"{raw_buffer} {buffer_units_canonical}"
            if buffer_m > 0
            else "none"
        )
        lines = [
            f"## Aggregation: {source_item_id} -> {aggregation_item_id}",
            f"**City:** {city}  |  **Group field:** `{group_by_field}`",
            f"**Source geometry:** {source_geom_type}  |  "
            f"**Centroid mode:** {centroid_mode}  |  "
            f"**Overlap policy:** {overlap_policy}  |  "
            f"**Proximity buffer:** {buffer_label}",
            f"**Source features:** {len(source_points):,}  |  "
            f"**Buckets:** {len(bucket_list)}  |  "
            f"**Unmatched:** {unmatched_count:,}",
            "",
        ]
        if not bucket_list:
            lines.append(
                "_No source features fell inside any aggregation polygon._"
            )
            return "\n".join(lines)

        header_cols = ["Group"]
        if include_count:
            header_cols.append("Count")
        header_cols.extend(sum_fields)
        lines.append("| " + " | ".join(header_cols) + " |")
        lines.append("|" + "---|" * len(header_cols))
        small_bucket_seen = False
        for group, bucket in bucket_list:
            row = [str(group)]
            if include_count:
                cnt = bucket["count"]
                if cnt < self.SMALL_SAMPLE_THRESHOLD:
                    small_bucket_seen = True
                    row.append(f"{cnt:,} (small sample)")
                else:
                    row.append(f"{cnt:,}")
            for fld in sum_fields:
                val = bucket[fld]
                if isinstance(val, float) and val.is_integer():
                    row.append(f"{int(val):,}")
                else:
                    row.append(f"{val:,}")
            lines.append("| " + " | ".join(row) + " |")
        if small_bucket_seen:
            lines += [
                "",
                f"_Buckets tagged_ **(small sample)** _hold fewer than "
                f"{self.SMALL_SAMPLE_THRESHOLD} source features. "
                f"Percentages and shares computed on these buckets are "
                f"weak -- name the count, not the percent, when "
                f"summarizing._",
            ]

        if unmatched_count:
            outside_what = (
                f"farther than {buffer_label} from every aggregation "
                f"polygon"
                if buffer_m > 0
                else "outside every aggregation polygon"
            )
            lines += [
                "",
                f"_{unmatched_count:,} source feature(s) fell "
                f"{outside_what}. This usually indicates "
                f"data-quality signal (stray coordinates, records "
                f"outside the city boundary)._",
            ]
        if len(source_features) >= max_source:
            lines += [
                "",
                f"_Source fetch hit the {max_source:,}-feature cap. "
                f"Narrow source_where to get a complete picture._",
            ]
        return "\n".join(lines)

    async def _filter_by_polygon(self, args: Dict[str, Any]) -> str:
        source_item_id = self._validate_item_id(
            (args.get("source_item_id") or "").strip()
        )
        container_item_id = self._validate_item_id(
            (args.get("container_item_id") or "").strip()
        )
        container_where = (args.get("container_where") or "").strip()
        if not container_where:
            raise ValueError(
                "container_where is required -- it identifies which "
                "polygon(s) in the container layer to filter against"
            )
        validated_container_where = WhereValidator.validate(container_where)
        # Validate source_where up front too -- rejection shouldn't wait
        # for the container-layer lookup to complete.
        source_where = WhereValidator.validate(
            args.get("source_where") or "1=1"
        )
        out_fields = args.get("out_fields", "*")
        return_geometry = bool(args.get("return_geometry", False))
        requested_limit = int(args.get("limit", 100))
        effective_limit = (
            min(requested_limit, self.GEOMETRY_LIMIT_CAP)
            if return_geometry
            else min(requested_limit, 1000)
        )

        # Resolve the container polygon(s) up-front so we can report a
        # friendly 0-match error instead of silently returning no records.
        container_url = await self._resolve_layer_url(container_item_id)
        container_meta = await self._fetch_layer_meta(container_url)
        geom_type = container_meta.get("geometryType", "")
        if geom_type not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"container_item_id must point at a polygon layer "
                f"(got geometryType={geom_type or 'unknown'!r})"
            )

        count_resp = await self.client.get(
            f"{container_url}/query",
            params={
                "where": validated_container_where,
                "returnCountOnly": "true",
                "f": "json",
            },
        )
        count_resp.raise_for_status()
        count_data = count_resp.json()
        if "error" in count_data:
            raise RuntimeError(
                count_data["error"].get(
                    "message", str(count_data["error"])
                )
            )
        matched_polygons = int(count_data.get("count") or 0)
        if matched_polygons == 0:
            return (
                f"Error: container_where {container_where!r} matched 0 "
                f"polygons on container layer `{container_item_id}`. "
                f"Did you misspell a name? Try "
                f"`search_spatial_layers` or `query_data` to browse "
                f"valid values for the container field."
            )

        # Delegate the actual spatial query to spatial_query_polygon's
        # filter-item pathway so we get server-side intersection and
        # consistent result formatting with the rest of the plugin.
        records = await self.spatial_query_polygon(
            source_item_id,
            filter_geometry=None,
            filter_item_id=container_item_id,
            filter_where=validated_container_where,
            spatial_rel="intersects",
            filters={
                "where": source_where,
                "out_fields": out_fields,
            },
            limit=effective_limit,
            return_geometry=return_geometry,
        )

        city = self.plugin_config.city_name
        header = (
            f"## Filter: {source_item_id} inside "
            f"{container_item_id} where {container_where!r}\n"
            f"**City:** {city}  |  "
            f"**Container polygons matched:** {matched_polygons:,}\n\n"
        )
        if not records:
            return (
                header
                + f"No features in `{source_item_id}` fall inside the "
                f"selected polygon(s)."
            )
        body = self._format_query_results(
            records, effective_limit, total_count=None, date_fields=None
        )
        return header + body

    async def _get_distinct_values(self, args: Dict[str, Any]) -> str:
        """Return distinct values of a field -- for confirming exact
        identifier/code formats before constructing a WHERE clause."""
        item_id = self._validate_item_id(
            (args.get("item_id") or "").strip()
        )
        field = (args.get("field") or "").strip()
        if not field:
            raise ValueError("field is required")
        like = (args.get("like") or "").strip()
        where = (args.get("where") or "1=1").strip()
        limit = min(int(args.get("limit", 50)), 500)

        layer_url = await self._resolve_layer_url(item_id)
        meta = await self._fetch_layer_meta(layer_url)
        field_names = {f.get("name") for f in meta.get("fields", [])}
        if field not in field_names:
            raise ValueError(
                f"field {field!r} is not a field on this layer. "
                f"Call `get_layer_schema(item_id='{item_id}')` to "
                f"see valid names -- they are CASE-SENSITIVE. "
                f"Available: {sorted(field_names)[:12]}..."
            )

        # Splice the LIKE clause into the user's WHERE before validation
        # so any injected SQL gets rejected by WhereValidator together
        # with the rest. Single-quote escape is local to LIKE-substring
        # construction; not a substitute for WhereValidator.
        if like:
            safe_like = like.replace("'", "''")
            like_clause = f"{field} LIKE '%{safe_like}%'"
            where = (
                f"({where}) AND ({like_clause})"
                if where != "1=1"
                else like_clause
            )
        where = WhereValidator.validate(where)

        # returnDistinctValues only works when returnGeometry=false on
        # most hosted Feature Services -- geometry coords break the
        # de-dup hash. We also dedupe client-side as a backstop in case
        # the upstream returns duplicates anyway (older portals do).
        params = {
            "f": "json",
            "where": where,
            "outFields": field,
            "returnDistinctValues": "true",
            "returnGeometry": "false",
            "orderByFields": field,
            "resultRecordCount": str(limit),
        }
        resp = await self.client.get(
            f"{layer_url}/query", params=params
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                "Distinct-values query failed: "
                + self._rewrite_arcgis_error(
                    err.get("message", "Unknown error"),
                    err.get("details", []),
                    resource_id=item_id,
                    has_where=True,
                )
            )

        seen: set = set()
        values: List[Any] = []
        for f in data.get("features", []):
            v = (f.get("attributes") or {}).get(field)
            if v is None or v in seen:
                continue
            seen.add(v)
            values.append(v)

        if not values:
            suffix = f" containing '{like}'" if like else ""
            return (
                f"No distinct values for field `{field}`{suffix} on "
                f"item `{item_id}` (where: {where!r}). "
                + (
                    "Try a different `like` substring, or omit `like` "
                    "to see all values."
                    if like
                    else "The layer may have no records, or every "
                    "value may be NULL."
                )
            )

        capped_note = (
            " (truncated to limit)" if len(values) >= limit else ""
        )
        lines = [
            f"## Distinct `{field}` values "
            f"({len(values)}{capped_note}, limit={limit})"
        ]
        if like:
            lines.append(
                f"_Filtered to values containing '{like}' "
                f"(case-sensitive)._"
            )
        lines.append("")
        for v in values:
            lines.append(f"- `{v}`")
        lines += [
            "",
            "---",
            "**NEXT STEP:** these are the EXACT values the layer "
            "stores -- copy them verbatim into a WHERE clause. Field "
            "and value matching is CASE-SENSITIVE.",
            f"Example: `query_data(item_id='{item_id}', "
            f"where=\"{field}='<paste a value above>'\", limit=1)` "
            "to count records with that value.",
        ]
        return "\n".join(lines)

    async def _find_parcel(self, args: Dict[str, Any]) -> str:
        """Look up a parcel across MOA format variants in one call.

        The same parcel can appear in different layers as
        ``001-213-29``, ``00121329``, ``003-184-87-000``, etc. The
        model rarely knows which form a given layer uses; this tool
        generates the four canonical forms from the input and tries
        them all in a single ``WHERE field IN (...)``. If none match,
        it falls back to a ``LIKE`` query on a distinctive substring
        and returns up to 5 candidates the model can inspect.
        """
        item_id = self._validate_item_id(
            (args.get("item_id") or "").strip()
        )
        parcel_field = (args.get("parcel_field") or "").strip()
        if not parcel_field:
            raise ValueError("parcel_field is required")
        parcel_id = (args.get("parcel_id") or "").strip()
        if not parcel_id:
            raise ValueError("parcel_id is required")
        out_fields = OutFieldsValidator.validate(
            args.get("out_fields") or "*"
        )
        limit = min(int(args.get("limit", 10)), 100)

        layer_url = await self._resolve_layer_url(item_id)
        meta = await self._fetch_layer_meta(layer_url)
        field_names = {f.get("name") for f in meta.get("fields", [])}
        if parcel_field not in field_names:
            raise ValueError(
                f"parcel_field {parcel_field!r} is not a field on "
                f"this layer. Call `get_layer_schema(item_id="
                f"'{item_id}', keyword='parcel')` to find the right "
                f"field name. Common MOA parcel field names: "
                f"`Parcel_Num`, `Name`, `Parcel_ID`, `GIS_ParcelNum8`, "
                f"`GIS_ParcelNum11`, `GIS_ParcelNum8Formatted`, "
                f"`GIS_ParcelNum11Formatted`."
            )

        variants = self._normalize_parcel_variants(parcel_id)
        if not variants:
            raise ValueError(
                f"Could not extract any digits from parcel_id "
                f"{parcel_id!r}. Provide a parcel ID like "
                f"'001-213-29', '00121329', or '00121329000'."
            )

        # Build IN clause; SQL-quote each variant with '' escape.
        quoted = ",".join(
            "'" + v.replace("'", "''") + "'" for v in variants
        )
        where_in = WhereValidator.validate(
            f"{parcel_field} IN ({quoted})"
        )

        params = {
            "f": "json",
            "where": where_in,
            "outFields": out_fields,
            "returnGeometry": "false",
            "resultRecordCount": str(limit),
        }
        resp = await self.client.get(
            f"{layer_url}/query", params=params
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                "Parcel lookup query failed: "
                + self._rewrite_arcgis_error(
                    err.get("message", "Unknown error"),
                    err.get("details", []),
                    resource_id=item_id,
                    has_where=True,
                )
            )

        features = data.get("features", [])

        if features:
            matched_values: set = set()
            for f in features:
                v = (f.get("attributes") or {}).get(parcel_field)
                if v is not None:
                    matched_values.add(v)
            lines = [
                f"## Parcel lookup: `{parcel_id}` -> "
                f"{len(features)} record(s) found",
                f"**Layer:** `{item_id}`",
                f"**Field:** `{parcel_field}`",
                f"**Variants tried ({len(variants)}):** "
                + ", ".join(f"`{v}`" for v in variants),
            ]
            if matched_values:
                canonical = sorted(matched_values)[0]
                lines.append(
                    "**Matched stored format(s):** "
                    + ", ".join(
                        f"`{v}`" for v in sorted(matched_values)
                    )
                )
                lines.append(
                    f"**Canonical form for this layer:** "
                    f"`{canonical}` -- use this verbatim for "
                    f"follow-up queries here."
                )
            lines.append("")
            for i, f in enumerate(features, 1):
                attrs = f.get("attributes") or {}
                lines.append(f"### Record {i}")
                for k, v in attrs.items():
                    lines.append(f"  {k}: {v}")
                lines.append("")
            return "\n".join(lines)

        # Not found via exact match. Fall back to LIKE on a distinctive
        # digit substring. Skip the first 3 chars (often leading
        # zeros + low-info prefix) to maximise selectivity.
        digits = "".join(c for c in parcel_id if c.isdigit())
        candidates: List[Dict[str, Any]] = []
        substring_used = ""
        if len(digits) >= 5:
            # Pick a 6-char window starting after any leading zeros for
            # distinctiveness; fall back to the longest available.
            stripped = digits.lstrip("0")
            substring_used = (
                stripped[:6] if len(stripped) >= 6 else stripped
            )
            if substring_used:
                safe_sub = substring_used.replace("'", "''")
                like_where = WhereValidator.validate(
                    f"{parcel_field} LIKE '%{safe_sub}%'"
                )
                try:
                    like_resp = await self.client.get(
                        f"{layer_url}/query",
                        params={
                            "f": "json",
                            "where": like_where,
                            "outFields": parcel_field,
                            "returnGeometry": "false",
                            "resultRecordCount": "5",
                        },
                    )
                    like_resp.raise_for_status()
                    like_data = like_resp.json()
                    if "error" not in like_data:
                        candidates = like_data.get("features") or []
                except Exception:
                    candidates = []

        lines = [
            f"## Parcel lookup: no exact match for `{parcel_id}`",
            f"**Layer:** `{item_id}`",
            f"**Field:** `{parcel_field}`",
            f"**Tried variants ({len(variants)}):** "
            + ", ".join(f"`{v}`" for v in variants),
            "",
        ]
        if candidates:
            lines.append(
                f"**LIKE fallback** with substring "
                f"`%{substring_used}%` returned "
                f"{len(candidates)} candidate(s):"
            )
            lines.append("")
            for c in candidates:
                attrs = c.get("attributes") or {}
                v = attrs.get(parcel_field)
                lines.append(f"- `{v}`")
            lines += [
                "",
                "_Pick the right candidate above and use its EXACT "
                "value for follow-up queries on this layer._",
            ]
        else:
            lines += [
                "_No candidates via LIKE fallback either. The parcel "
                "may not exist in this layer, or the field stores the "
                "ID in an unrecognised format. Try "
                f"`get_distinct_values(item_id='{item_id}', "
                f"field='{parcel_field}', "
                f"like='{substring_used or digits[:6]}')` to discover "
                f"the storage format._",
            ]
        return "\n".join(lines)

    async def _find_features_spanning_classifications(
        self, args: Dict[str, Any]
    ) -> str:
        """Find source features whose footprint touches >= min_distinct
        distinct values of a classification field in another polygon
        layer. The "split-zoned parcel" pattern, generalised."""
        source_item_id = self._validate_item_id(
            (args.get("source_item_id") or "").strip()
        )
        classification_item_id = self._validate_item_id(
            (args.get("classification_item_id") or "").strip()
        )
        classification_field = (
            args.get("classification_field") or ""
        ).strip()
        if not classification_field:
            raise ValueError("classification_field is required")

        # Enforce min_distinct >= 2 -- anything less is "any feature
        # touching a classification", which is just spatial_query_polygon.
        min_distinct = max(2, int(args.get("min_distinct", 2)))
        source_where = WhereValidator.validate(
            args.get("source_where") or "1=1"
        )
        classification_where = WhereValidator.validate(
            args.get("classification_where") or "1=1"
        )
        out_fields = OutFieldsValidator.validate(
            args.get("out_fields") or "*"
        )
        limit = min(int(args.get("limit", 100)), 1000)
        max_source = min(
            int(
                args.get(
                    "max_source_features", self.SPANNING_SOURCE_LIMIT
                )
            ),
            self.SPANNING_SOURCE_LIMIT,
        )

        # Self-intersection makes no sense -- every feature touches
        # itself, every feature qualifies, the answer is trivially
        # "all of them". A weak model occasionally picks the same
        # layer for both args when reasoning is muddled; refuse fast
        # so we don't waste a spatial loop and confuse the user.
        if source_item_id == classification_item_id:
            raise ValueError(
                "source_item_id and classification_item_id are the "
                "same layer. Self-intersection is meaningless -- "
                "every feature trivially touches itself. For a "
                "'parcels spanning multiple zones' question, source "
                "should be a parcels layer (e.g. MOA_Parcels_Hosted, "
                "TaxParcels_Hosted) and classification should be a "
                "zoning layer (e.g. Zoning_Hosted). Use "
                "`find_gis_content(topic='parcels')` and "
                "`find_gis_content(topic='zoning')` to discover the "
                "right item IDs for each side."
            )

        source_url = await self._resolve_layer_url(source_item_id)
        classification_url = await self._resolve_layer_url(
            classification_item_id
        )
        cls_meta = await self._fetch_layer_meta(classification_url)
        cls_geom = cls_meta.get("geometryType", "")
        if cls_geom not in (
            "esriGeometryPolygon",
            "esriGeometryMultiPatch",
        ):
            raise ValueError(
                f"classification_item_id must point at a polygon "
                f"layer (got geometryType={cls_geom or 'unknown'!r}). "
                f"Spanning analysis needs polygons to define the "
                f"distinct regions a source feature might cross."
            )
        cls_field_names = {
            f.get("name") for f in cls_meta.get("fields", [])
        }
        if classification_field not in cls_field_names:
            raise ValueError(
                f"classification_field {classification_field!r} is "
                f"not a field on the classification layer. Call "
                f"`get_layer_schema(item_id="
                f"'{classification_item_id}')` to see valid names -- "
                f"they are CASE-SENSITIVE. Available: "
                f"{sorted(cls_field_names)[:12]}..."
            )

        # Wrong-grain classification detection. A classification layer
        # that has parcel-identifier fields is itself a per-parcel
        # layer (one polygon per legal parcel) -- its `Zoning_District`
        # or similar field is an attribute on each parcel, NOT a
        # categorisation across few polygons. Spatial-intersecting
        # parcels against parcels mostly produces "0 qualifying"
        # because of the 1,000-polygon classification cap. The right
        # classification is a dedicated zoning-polygon layer. If the
        # caller deliberately wants to span on another parcel-grain
        # field, they can pass that field explicitly -- only refuse
        # when classification_field is NOT itself a parcel-id
        # (otherwise we'd block the legit "find parcels with multiple
        # parcel IDs" sanity-check use case).
        cls_is_parcel_grain = bool(
            self.PARCEL_INDICATOR_FIELDS & cls_field_names
        )
        if (
            cls_is_parcel_grain
            and classification_field not in self.PARCEL_INDICATOR_FIELDS
        ):
            parcel_fields_present = sorted(
                self.PARCEL_INDICATOR_FIELDS & cls_field_names
            )
            raise ValueError(
                f"classification_item_id `{classification_item_id}` "
                f"looks like a per-parcel layer -- it has parcel "
                f"identifier field(s) "
                f"{parcel_fields_present}. That means each polygon is "
                f"one parcel, and `{classification_field}` is an "
                f"attribute stored on each parcel record (parcel "
                f"grain), NOT a categorisation across a small number "
                f"of zone polygons (zone grain).\n\n"
                f"Spanning analysis needs the classification layer to "
                f"have FEW polygons (zones, districts, regions) where "
                f"many polygons share each distinct value. Otherwise "
                f"the upstream cap of "
                f"{self.SPANNING_CLASSIFICATION_LIMIT} polygons "
                f"forces a random sample and the spatial join misses "
                f"most matches.\n\n"
                f"For zoning specifically, use the dedicated zoning "
                f"layer -- call `find_gis_content(topic='zoning')` "
                f"and pick a Feature Service whose name is "
                f"`Zoning_Hosted` or similar (NOT a parcels or "
                f"property-information layer). Confirm with "
                f"`get_layer_schema` that the layer has a "
                f"`GeneralizedZone` or `ZONING_DESIGNATIONS` field "
                f"and only ~1,000 polygons total."
            )

        # Source layer must have a user-facing identifier field, OR
        # the caller must have explicitly chosen out_fields (which
        # signals "I know what fields this layer has").
        # Without this check, a model that picks an aggregate layer
        # (zoning districts, council areas) as source produces output
        # that lists OBJECTIDs as "parcel numbers" -- the exact bug
        # the user keeps hitting.
        if out_fields == "*":
            src_meta = await self._fetch_layer_meta(source_url)
            src_field_names = {
                f.get("name") for f in src_meta.get("fields", [])
            }
            has_natural = any(
                f in src_field_names
                for f in self.NATURAL_ID_FIELD_PRIORITY
            )
            if not has_natural:
                visible_fields = [
                    f for f in sorted(src_field_names)
                    if f
                    and not f.startswith("Shape__")
                    and f not in ("OBJECTID", "OID", "FID")
                ][:12]
                raise ValueError(
                    f"source_item_id `{source_item_id}` has no "
                    f"user-facing identifier field (no Parcel_ID, "
                    f"Name, Address, Title, etc.). Without one, "
                    f"results would only have internal OBJECTIDs -- "
                    f"unsafe to report as parcel numbers / "
                    f"addresses / names.\n\n"
                    f"This layer's non-shape fields: "
                    f"{visible_fields or '(none)'}.\n\n"
                    f"This is most likely a wrong layer choice -- "
                    f"aggregate / boundary layers (zoning districts, "
                    f"council areas, regions) typically have only "
                    f"OBJECTID + Shape fields and are meant to be "
                    f"the CLASSIFICATION, not the source. For "
                    f"'parcels spanning multiple X' use "
                    f"`find_gis_content(topic='parcels')` and pick "
                    f"a layer with `Parcel_ID` (e.g. "
                    f"MOA_Parcels_Hosted, TaxParcels_Hosted, "
                    f"PropertyInformation_Hosted) from the "
                    f"QUERYABLE section. To override and proceed "
                    f"anyway, pass `out_fields` explicitly."
                )

        # Pre-flight: refuse if the source layer has more features
        # matching source_where than the cap. Doing this up front saves
        # the per-classification spatial-query round-trips that would
        # otherwise be wasted.
        source_count = await self._get_record_count(
            source_url, source_where
        )
        if source_count is None:
            source_count = 0
        if source_count > max_source:
            raise ValueError(
                f"source layer has {source_count:,} features matching "
                f"source_where, exceeding the cap of "
                f"{max_source:,}. Narrow `source_where` to fit under "
                f"the cap (e.g. limit by neighborhood, zone, or "
                f"region). The cap exists to bound per-call compute "
                f"on the upstream portal."
            )
        if source_count == 0:
            return (
                f"source layer has 0 features matching source_where "
                f"({source_where!r}). Nothing to spatially join "
                f"against."
            )

        cls_polys = await self._paged_geojson_fetch(
            classification_url,
            where=classification_where,
            out_fields=classification_field,
            limit=self.SPANNING_CLASSIFICATION_LIMIT,
        )
        if not cls_polys:
            raise ValueError(
                f"classification_where ({classification_where!r}) "
                f"matched no polygons in the classification layer. "
                f"Verify with `query_data(item_id="
                f"'{classification_item_id}', limit=1)`."
            )
        # Detect cap-hit. The fetch tops out at SPANNING_CLASSIFICATION
        # _LIMIT -- anything >= 95% of cap probably means there are more
        # polygons than we sampled, and the spatial join is operating
        # on a partial view. Surface this so the model knows results
        # are not exhaustive.
        cls_cap_hit = (
            len(cls_polys) >= 0.95 * self.SPANNING_CLASSIFICATION_LIMIT
        )

        # Drop polygons whose classification value is NULL -- they would
        # falsely contribute "no value" as a distinct classification.
        valid_cls: List[Tuple[Any, Dict[str, Any]]] = []
        for p in cls_polys:
            val = (p.get("properties") or {}).get(classification_field)
            geom = p.get("geometry")
            if val is None or not geom:
                continue
            valid_cls.append((val, geom))
        if not valid_cls:
            raise ValueError(
                f"all classification polygons matching "
                f"classification_where have NULL "
                f"`{classification_field}` values or missing "
                f"geometry. Pick a different field or narrow "
                f"classification_where."
            )

        # Group polygons by their classification value so we send ONE
        # spatial query per distinct value instead of one per polygon.
        # For a typical zoning layer with 1,000 polygons but ~50 zones,
        # this collapses 1,000 upstream queries into 50 -- the original
        # implementation hit the Esri 6,000-req/min quota easily and
        # the silent failure path then masked the rate-limit error as
        # "attributes unavailable". One query per value is also more
        # honest semantically: we want distinct VALUES per source
        # feature, not distinct polygons.
        by_value: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
        for value, geom in valid_cls:
            by_value[value].append(geom)

        sem = asyncio.Semaphore(self.SPANNING_QUERY_CONCURRENCY)
        src_to_values: Dict[Any, set] = defaultdict(set)
        skipped_polys = 0
        rate_limited = False

        async def query_geometry(
            value: Any, esri_geom: Dict[str, Any]
        ) -> None:
            nonlocal skipped_polys, rate_limited
            params = {
                "where": source_where,
                "geometry": json.dumps(
                    esri_geom, separators=(",", ":")
                ),
                "geometryType": "esriGeometryPolygon",
                "spatialRel": "esriSpatialRelIntersects",
                "inSR": "4326",
                "returnIdsOnly": "true",
                "f": "json",
            }
            async with sem:
                try:
                    resp = await self.client.post(
                        f"{source_url}/query", data=params
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    skipped_polys += 1
                    return
            if "error" in data:
                err_msg = (
                    data["error"].get("message", "") or ""
                ).lower()
                # Esri uses both phrasings depending on the tier.
                if (
                    "too many requests" in err_msg
                    or "quota exceeded" in err_msg
                ):
                    rate_limited = True
                skipped_polys += 1
                return
            for oid in data.get("objectIds") or []:
                src_to_values[oid].add(value)

        async def query_one_value(
            value: Any, geoms: List[Dict[str, Any]]
        ) -> None:
            """Combine all polygons for this value into one query
            when feasible; fall back to per-polygon if the combined
            geometry exceeds the filter caps."""
            nonlocal skipped_polys
            all_rings: List[Any] = []
            skipped_in_value = 0
            for geom in geoms:
                try:
                    esri = self._geojson_to_esri_polygon(geom)
                except ValueError:
                    skipped_in_value += 1
                    continue
                all_rings.extend(esri.get("rings") or [])
            if not all_rings:
                skipped_polys += skipped_in_value
                return
            coord_count = sum(
                len(r) for r in all_rings if isinstance(r, list)
            )
            if (
                len(all_rings) <= self.MAX_FILTER_RINGS
                and coord_count <= self.MAX_FILTER_COORDS
            ):
                # Combined fits -- one query for the whole value.
                combined = {
                    "rings": all_rings,
                    "spatialReference": {"wkid": 4326},
                }
                skipped_polys += skipped_in_value
                await query_geometry(value, combined)
                return
            # Combined too big -- fall back to per-polygon for this
            # value. Rare, but real for sprawling zone categories.
            skipped_polys += skipped_in_value
            for geom in geoms:
                try:
                    esri = self._geojson_to_esri_polygon(geom)
                except ValueError:
                    skipped_polys += 1
                    continue
                await query_geometry(value, esri)

        await asyncio.gather(
            *[
                query_one_value(v, gs)
                for v, gs in by_value.items()
            ]
        )

        qualifying = {
            oid: vals
            for oid, vals in src_to_values.items()
            if len(vals) >= min_distinct
        }

        # Histogram covers ALL source features that touched any
        # classification, not just qualifiers -- gives the model context
        # about the distribution before highlighting the cutoff.
        histogram = Counter(
            len(vals) for vals in src_to_values.values()
        )

        city = self.plugin_config.city_name
        lines = [
            f"## Source features spanning multiple "
            f"`{classification_field}` values",
            f"**City:** {city}",
            f"**Source:** {source_item_id} "
            f"({source_count:,} features matching source_where)",
            f"**Classification:** {classification_item_id}, field "
            f"`{classification_field}` "
            f"({len(valid_cls):,} polygon(s) with non-null value)",
            f"**Threshold:** features touching "
            f">= {min_distinct} distinct values qualify",
            f"**Qualifying:** {len(qualifying):,} feature(s)",
        ]
        if skipped_polys:
            note = (
                "Coverage is partial; narrow "
                "`classification_where` to inspect them separately."
            )
            if rate_limited:
                note = (
                    "**Upstream rate-limited some queries** -- "
                    "results below are partial. Wait 60s and retry, "
                    "or narrow `classification_where` to reduce the "
                    "per-call query count."
                )
            lines.append(
                f"**Skipped:** {skipped_polys} classification "
                f"polygon(s) -- too complex for spatial query payload "
                f"or upstream error. {note}"
            )
        if cls_cap_hit:
            lines.append(
                f"**Cap reached:** classification layer fetch "
                f"hit the {self.SPANNING_CLASSIFICATION_LIMIT:,}-"
                f"polygon cap -- results are based on a partial "
                f"sample. If qualifying count looks unexpectedly "
                f"low, the classification layer is probably at the "
                f"wrong grain (e.g., per-parcel instead of per-zone) "
                f"or has more polygons than the cap allows. Narrow "
                f"`classification_where` to a region or use a layer "
                f"with fewer, larger polygons."
            )
        lines.append("")

        if histogram:
            lines.append(
                "### Distribution of source features by distinct "
                "classification touch-count"
            )
            for count in sorted(histogram.keys()):
                n = histogram[count]
                marker = (
                    "  <- qualifying"
                    if count >= min_distinct
                    else ""
                )
                lines.append(
                    f"- touches {count} distinct value(s): "
                    f"{n:,} feature(s){marker}"
                )
            lines.append("")

        if not qualifying:
            lines.append(
                f"_No source features touch >= {min_distinct} "
                f"distinct `{classification_field}` values. "
                f"Try lowering `min_distinct`, broadening "
                f"`source_where`, or confirming the classification "
                f"layer has the expected boundary detail._"
            )
            return "\n".join(lines)

        # Fetch attributes for the qualifying subset, capped at limit.
        # Sort by descending distinct-value count so the most-split
        # features show first -- they tend to be the most interesting.
        qualifying_oids = sorted(
            qualifying.keys(),
            key=lambda o: (-len(qualifying[o]), o),
        )[:limit]

        # POST (not GET) for the attribute fetch -- with limit=100, the
        # objectIds list pushes the URL past portal length limits when
        # combined with where + outFields, and the upstream silently
        # truncated to zero features. POST sends the same params in
        # the body and is unaffected.
        attrs_resp = await self.client.post(
            f"{source_url}/query",
            data={
                "where": source_where,
                "objectIds": ",".join(map(str, qualifying_oids)),
                "outFields": out_fields,
                "returnGeometry": "false",
                "f": "json",
            },
        )
        attrs_resp.raise_for_status()
        attrs_data = attrs_resp.json()
        attrs_error: Optional[str] = None
        if "error" in attrs_data:
            err = attrs_data["error"]
            attrs_error = self._rewrite_arcgis_error(
                err.get("message", "Unknown error"),
                err.get("details", []),
                resource_id=source_item_id,
                has_out_fields=out_fields != "*",
                has_where=source_where != "1=1",
            )

        # Index by OID for join with the value sets. ArcGIS layers may
        # name the OID field OBJECTID, OID, or FID -- try all three.
        features_by_oid: Dict[Any, Dict[str, Any]] = {}
        if not attrs_error:
            for f in attrs_data.get("features", []):
                attrs = f.get("attributes") or {}
                oid = (
                    attrs.get("OBJECTID")
                    or attrs.get("OID")
                    or attrs.get("FID")
                )
                if oid is not None:
                    features_by_oid[oid] = attrs

        # Per-OID fallback: if the bulk fetch failed (rate limit, URL
        # length, transient upstream error) we MUST get attributes
        # before rendering. Weak models report OBJECTIDs as parcel
        # numbers when shown raw, no matter how loud the warning. Per-
        # OID queries are cheap (1 record each) and survive rate
        # limiting that kills the bulk path. Bounded concurrency to
        # stay polite to the upstream.
        fallback_used = False
        fallback_recovered = 0
        if not features_by_oid and qualifying_oids:
            fallback_used = True
            fallback_sem = asyncio.Semaphore(3)

            async def fetch_one_oid(oid: Any) -> None:
                # AND with source_where so the fallback honors the
                # same predicate as the bulk path.
                if source_where and source_where != "1=1":
                    where_one = f"({source_where}) AND OBJECTID={oid}"
                else:
                    where_one = f"OBJECTID={oid}"
                where_one = WhereValidator.validate(where_one)
                async with fallback_sem:
                    try:
                        r = await self.client.get(
                            f"{source_url}/query",
                            params={
                                "where": where_one,
                                "outFields": out_fields,
                                "returnGeometry": "false",
                                "f": "json",
                            },
                        )
                        r.raise_for_status()
                        d = r.json()
                    except Exception:
                        return
                    if "error" in d:
                        return
                    feats = d.get("features") or []
                    if feats:
                        attrs = feats[0].get("attributes") or {}
                        if attrs:
                            features_by_oid[oid] = attrs

            await asyncio.gather(
                *[fetch_one_oid(o) for o in qualifying_oids]
            )
            fallback_recovered = len(features_by_oid)

        showing = len(qualifying_oids)
        total = len(qualifying)
        lines.append(
            f"### Qualifying features (showing {showing:,} of "
            f"{total:,}, sorted by distinct-value count desc)"
        )
        if showing < total:
            lines.append(
                f"_Truncated to limit={limit}. Increase `limit` to "
                f"see more._"
            )
        if fallback_used and fallback_recovered:
            lines.append(
                f"_Bulk attribute fetch failed (likely upstream "
                f"rate limit); recovered {fallback_recovered:,} of "
                f"{showing:,} via per-OID fallback._"
            )

        # Detect the natural-ID field across the loaded attributes so
        # we can promote it to the lead position of each entry. Pick
        # the first feature's natural ID -- schemas are consistent
        # within a layer, so any one feature's pick applies to all.
        natural_id_field: Optional[str] = None
        for attrs in features_by_oid.values():
            pick = self._pick_natural_id(attrs)
            if pick:
                natural_id_field = pick[0]
                break
        if natural_id_field:
            lines.append("")
            lines.append(
                f"> **The user-facing identifier in this layer is "
                f"`{natural_id_field}`** -- that is the value to "
                f"REPORT TO THE USER for each feature below. "
                f"`OBJECTID` is an internal layer row ID; do NOT "
                f"report it as a parcel number, address, or name."
            )
        lines.append("")

        # If even the per-OID fallback couldn't load attributes, we
        # must NOT surface raw OBJECTIDs -- a model can report them as
        # parcel numbers no matter how loud the warning sits next to
        # them, so we don't hand over the raw IDs at all. Return a clean
        # refusal with concrete recovery steps for the model to give the
        # user, instead of dangerous raw data.
        if not features_by_oid and qualifying_oids:
            reason = (
                attrs_error
                or "the upstream returned 0 records for the OIDs "
                "sent, AND the per-OID fallback also failed. Most "
                "common cause is a sustained rate-limit from the "
                "Esri portal."
            )
            lines.append(
                "> **CANNOT RETURN PARCEL NUMBERS RIGHT NOW.** "
                f"The spanning analysis identified {total:,} "
                "qualifying features, but the upstream layer would "
                "not return their attributes (the user-facing "
                "identifiers).\n"
                f">\n"
                f"> Reason: {reason}\n"
                f">\n"
                f"> **DO NOT report internal OBJECTIDs to the user "
                f"as parcel numbers** -- they are not parcel numbers, "
                f"addresses, or any identifier the user would "
                f"recognize. Tell the user the upstream is "
                f"temporarily rate-limiting bulk lookups and "
                f"suggest one of: (1) retry in 60 seconds, (2) "
                f"narrow the request to a smaller area via "
                f"`source_where`, or (3) ask for fewer results "
                f"(`limit=5`)."
            )
            return "\n".join(lines)

        for oid in qualifying_oids:
            attrs = features_by_oid.get(oid)
            vals = sorted(str(v) for v in qualifying[oid])
            if attrs is None:
                # Per-row miss in an otherwise-successful response.
                # Still surface it honestly rather than silently.
                lines.append(
                    f"**OBJECTID {oid}** _(attributes not returned "
                    f"by upstream -- internal row ID only, NOT a "
                    f"user-facing parcel number)_ -- touches "
                    f"{len(vals)} value(s): "
                    + ", ".join(f"`{v}`" for v in vals)
                )
                lines.append("")
                continue
            # Lead with the user-facing identifier when one is
            # detectable; demote OBJECTID to a parenthetical so the
            # model never reports it as the answer.
            natural = self._pick_natural_id(attrs)
            if natural:
                nat_field, nat_value = natural
                lines.append(
                    f"**`{nat_value}`** ({nat_field}; internal "
                    f"OBJECTID {oid}) -- touches {len(vals)} "
                    f"value(s): "
                    + ", ".join(f"`{v}`" for v in vals)
                )
            else:
                lines.append(
                    f"**OBJECTID {oid}** -- touches {len(vals)} "
                    f"value(s): "
                    + ", ".join(f"`{v}`" for v in vals)
                )
            for k, v in attrs.items():
                if k in ("OBJECTID", "OID", "FID"):
                    continue
                lines.append(f"  {k}: {v}")
            lines.append("")

        lines += [
            "---",
            "_Values shown use the layer's native format -- "
            "case-sensitive. Pass them verbatim if you filter on "
            "them next._",
        ]
        return "\n".join(lines)

    # ── Tool definitions ──────────────────────────────────────────────────

    def get_tools(self) -> List[ToolDefinition]:
        city = (
            self.plugin_config.city_name if self.plugin_config else "Unknown"
        )
        tools = [
            ToolDefinition(
                name="find_gis_content",
                description=(
                    f"START HERE for any {city} GIS question. Searches "
                    f"the GIS portal for maps, apps, and datasets on a "
                    f"topic (e.g. 'flood zones', 'trails', 'parks'). "
                    f"Returns items each with an `id`; follow up with "
                    f"`query_data(item_id, limit=1)` for counts, "
                    f"`query_data(item_id, where=..., limit=N)` for "
                    f"listings, or `get_layer_schema(item_id)` to see "
                    f"real field names before writing a WHERE clause."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": (
                                "Topic to search for. REQUIRED -- extract "
                                "from the user's question. Use the "
                                "simplest 1-2 word form: 'parks' (not "
                                "'park parcels'), 'flood' (not 'flood "
                                "zone boundaries'), 'trails', 'zoning', "
                                "'schools'. If a multi-word topic returns "
                                "nothing, retry with the most distinctive "
                                "single word."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results per source (default 8).",
                            "default": 8,
                        },
                    },
                    "required": ["topic"],
                },
            ),
            ToolDefinition(
                name="browse_gallery",
                description=(
                    f"Browse or search {city}'s curated public GIS "
                    f"gallery -- interactive maps, dashboards, apps, "
                    f"and StoryMaps. Optionally filter by keyword.\n\n"
                    f"NOTE: gallery items are VIEWERS (Web Maps, "
                    f"Dashboards, Apps), NOT queryable data. Use this "
                    f"tool only when the user wants to *visit a "
                    f"viewer* -- e.g. 'show me the parks app', "
                    f"'where's the flood zone map?'. For 'how many?' "
                    f"or 'list X' questions you need queryable data: "
                    f"call `find_gis_content(topic=...)` instead and "
                    f"pick a Feature Service from the QUERYABLE "
                    f"section."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "Optional keyword to filter gallery items.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 20).",
                            "default": 20,
                        },
                    },
                },
            ),
            ToolDefinition(
                name="search_spatial_layers",
                description=(
                    f"Search {city}'s ArcGIS Online for raw spatial layers -- "
                    f"Feature Services, Map Services, tile layers, Web Maps, "
                    f"and downloadable data (GeoJSON, Shapefile, CSV). Use "
                    f"when the user wants underlying GIS data rather than a "
                    f"pre-built viewer. Prefer `find_gis_content` for "
                    f"general questions -- it also searches the public "
                    f"gallery. Use this tool when you specifically need a "
                    f"queryable layer to pass to `query_data`. Use simple "
                    f"1-2 word keywords (e.g. 'parks' not 'park parcels')."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keyword search query.",
                        },
                        "layer_type": {
                            "type": "string",
                            "enum": ["layers", "data", "all"],
                            "description": (
                                "'layers' = Feature/Map/Image Services and "
                                "tile layers; 'data' = Web Maps, GeoJSON, "
                                "Shapefile, CSV; 'all' = both."
                            ),
                            "default": "all",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 10, max 50).",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolDefinition(
                name="get_item_details",
                description=(
                    f"Get full details for a specific {city} GIS item by its "
                    f"ArcGIS item ID: title, full description, tags, owner, "
                    f"dates, service URL, and spatial extent."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID "
                                "(32-character hex string)."
                            ),
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="get_layer_schema",
                description=(
                    "Fetch the schema (field names, types, aliases, coded "
                    "domains) for a Feature Service or Map Service layer. "
                    "Use to discover what attributes a dataset contains. "
                    "Accepts an ArcGIS item ID or a direct service URL."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID. "
                                "Use this or service_url."
                            ),
                        },
                        "service_url": {
                            "type": "string",
                            "description": (
                                "Direct ArcGIS REST service URL ending in "
                                "/FeatureServer or /FeatureServer/0."
                            ),
                        },
                        "layer_index": {
                            "type": "integer",
                            "description": (
                                "Layer index within the service (default 0)."
                            ),
                            "default": 0,
                        },
                        "keyword": {
                            "type": "string",
                            "description": (
                                "Only show fields whose name/alias "
                                "contains this term."
                            ),
                        },
                    },
                },
            ),
            ToolDefinition(
                name="get_distinct_values",
                description=(
                    f"List the distinct values in a {city} Feature "
                    f"Service field -- to confirm EXACT format of "
                    f"identifiers/codes before writing a WHERE clause. "
                    f"Catches variants like 'R2M' vs 'R-2M', "
                    f"'1-213-29' vs '0012132900', 'Fairview' vs "
                    f"'FAIRVIEW'. Optional `like` substring-narrows. "
                    f"Chain: get_layer_schema -> get_distinct_values "
                    f"-> query_data with the verified value."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of a queryable "
                                "Feature/Map Service."
                            ),
                        },
                        "field": {
                            "type": "string",
                            "description": (
                                "Field name to get distinct values "
                                "for. CASE-SENSITIVE -- use the exact "
                                "name from `get_layer_schema`."
                            ),
                        },
                        "like": {
                            "type": "string",
                            "description": (
                                "Optional substring; only values "
                                "containing this string are returned. "
                                "E.g. like='2M' surfaces 'R-2M', "
                                "'R-2M (PUD)'."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE to narrow which "
                                "records contribute distinct values."
                            ),
                            "default": "1=1",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max distinct values to return "
                                "(default 50, max 500)."
                            ),
                            "default": 50,
                        },
                    },
                    "required": ["item_id", "field"],
                },
            ),
            ToolDefinition(
                name="find_parcel",
                description=(
                    f"Look up a {city} parcel across the 4 MOA format "
                    f"variants (`001-213-29`, `00121329`, "
                    f"`00121329000`, `003-184-87-000`) in one query. "
                    f"Pass any one form -- hyphens, leading zeros, "
                    f"and 'Parcel ' prefixes are normalized. Falls "
                    f"back to a LIKE search returning up to 5 "
                    f"candidates on no exact match. Pre-flight "
                    f"`get_layer_schema(item_id, keyword='parcel')` "
                    f"to pick the right `parcel_field` -- usually "
                    f"one of: Parcel_Num, Parcel_ID, GIS_ParcelNum8, "
                    f"GIS_ParcelNum11."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of a queryable "
                                "Feature/Map Service that has a "
                                "parcel-ID field."
                            ),
                        },
                        "parcel_field": {
                            "type": "string",
                            "description": (
                                "Name of the parcel-ID field on the "
                                "layer. CASE-SENSITIVE. Use "
                                "`get_layer_schema(item_id=<id>, "
                                "keyword='parcel')` to find it."
                            ),
                        },
                        "parcel_id": {
                            "type": "string",
                            "description": (
                                "The parcel ID in any common form: "
                                "'001-213-29', '00121329', "
                                "'00121329000', '003-184-87-000', "
                                "or even '1-213-29' (leading zeros "
                                "filled in). Hyphens and prefixes "
                                "are flexible."
                            ),
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to "
                                "return for matched records "
                                "(default '*')."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max records to return (default 10, "
                                "max 100)."
                            ),
                            "default": 10,
                        },
                    },
                    "required": [
                        "item_id",
                        "parcel_field",
                        "parcel_id",
                    ],
                },
            ),
            ToolDefinition(
                name="search_layers_by_field",
                description=(
                    f"Find {city} Feature Services that contain a specific "
                    f"field name or alias. Use to discover which datasets "
                    f"have a particular attribute -- e.g. 'flood', 'permit', "
                    f"'zone'."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "field_keyword": {
                            "type": "string",
                            "description": (
                                "Keyword to match in field names/aliases."
                            ),
                        },
                        "service_keyword": {
                            "type": "string",
                            "description": (
                                "Optional: pre-filter services by title "
                                "keyword. If omitted, `field_keyword` is "
                                "used as the catalog filter so the inspected "
                                "sample is biased toward relevant services "
                                "(the ArcGIS catalog is popularity-ranked, "
                                "so an unfiltered search often misses "
                                "less-trafficked services)."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max services to inspect "
                                "(default 10, max 20)."
                            ),
                            "default": 10,
                        },
                    },
                    "required": ["field_keyword"],
                },
            ),
            ToolDefinition(
                name="query_data",
                description=(
                    f"Query records from a {city} ArcGIS Feature "
                    f"Service. Field names are CASE-SENSITIVE -- use "
                    f"`get_layer_schema(item_id)` first; typos are "
                    f"caught with a 'did you mean' suggestion. "
                    f"COUNTING ('how many X?'): set `limit=1` and "
                    f"read the 'TOTAL COUNT' line. LISTING: set "
                    f"`limit` to how many records you want and "
                    f"optional `where` to filter (default 1=1 = all). "
                    f"Item must be a Feature/Map Service (not a Web "
                    f"Map or app)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": "ArcGIS Online item ID.",
                        },
                        "where": {
                            "type": "string",
                            "description": "SQL WHERE clause for filtering.",
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Maximum number of records (default 25). "
                                "Raise to 100+ for full listings."
                            ),
                            "default": 25,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "Field name to sort by, optionally followed "
                                "by ASC or DESC (e.g. 'DateOfAdoption DESC')."
                            ),
                        },
                        "date_format": {
                            "type": "string",
                            "enum": ["date", "epoch"],
                            "description": (
                                "'date' (default) returns date fields as "
                                "YYYY-MM-DD; 'epoch' keeps raw millisecond "
                                "timestamps for data pipeline use. "
                                "Ignored when return_geometry=true "
                                "(GeoJSON responses already use ISO 8601)."
                            ),
                            "default": "date",
                        },
                        "return_geometry": {
                            "type": "boolean",
                            "description": (
                                "If true, each record includes a GeoJSON "
                                "geometry in WGS84 (EPSG:4326), "
                                "server-side simplified to ~5m. Default "
                                "false. When true, limit is capped at 50 "
                                "to guard against polygon payload bloat. "
                                "For point-in-polygon lookups, prefer "
                                "spatial_query_point."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="spatial_query_point",
                description=(
                    f"Point-in-polygon lookup on a {city} polygon "
                    f"Feature Service. Given a lon/lat (WGS84), "
                    f"returns the attributes of every polygon "
                    f"containing the point ('which park / zone / "
                    f"flood zone is here?'). Call `get_layer_schema` "
                    f"first to confirm geometryType is "
                    f"esriGeometryPolygon and to see field names for "
                    f"the optional `where` / `out_fields`. Field "
                    f"names are CASE-SENSITIVE. Returns attributes "
                    f"only; no geometry."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID of a polygon "
                                "Feature Service."
                            ),
                        },
                        "lon": {
                            "type": "number",
                            "description": (
                                "Longitude in WGS84 decimal degrees "
                                "(-180 to 180). Note: lon comes first."
                            ),
                        },
                        "lat": {
                            "type": "number",
                            "description": (
                                "Latitude in WGS84 decimal degrees "
                                "(-90 to 90)."
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE clause to further "
                                "filter candidate features."
                            ),
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max features to return (default 10, "
                                "max 50). Point-in-polygon usually "
                                "returns 0-3 features."
                            ),
                            "default": 10,
                            "minimum": 1,
                            "maximum": 50,
                        },
                    },
                    "required": ["item_id", "lon", "lat"],
                },
            ),
            ToolDefinition(
                name="spatial_query_polygon",
                description=(
                    f"Server-side spatial intersection on a {city} "
                    f"Feature Service. Polygon filter is either inline "
                    f"GeoJSON (`filter_geometry`) or polygon feature(s) "
                    f"in another layer (`filter_item_id` + "
                    f"`filter_where`). Returns target features whose "
                    f"geometry intersects (or other `spatial_rel`) the "
                    f"filter. Target layer can be polygon, polyline, "
                    f"or point -- use this over centroid-based "
                    f"assignments when features can straddle "
                    f"boundaries. PROXIMITY ('within N miles/feet of "
                    f"X'): set `distance` + `units` to buffer the filter "
                    f"polygon server-side -- e.g. people within half a "
                    f"mile of a park = target a population layer, filter "
                    f"on the parks layer, distance=0.5, units='miles'. "
                    f"Call `get_layer_schema` on BOTH "
                    f"layers (target and filter) before writing "
                    f"`where`/`filter_where` -- they have different "
                    f"schemas. Set `return_geometry=true` for GeoJSON "
                    f"output if you need to clip client-side."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS Online item ID of the target "
                                "layer (the layer to select features "
                                "from)."
                            ),
                        },
                        "filter_item_id": {
                            "type": "string",
                            "description": (
                                "Item ID of a polygon layer whose "
                                "feature(s) will be used as the filter "
                                "geometry. Combine with filter_where to "
                                "pick specific features. Use this when "
                                "the filter polygon already exists as a "
                                "published layer -- avoids passing large "
                                "geometries through the model."
                            ),
                        },
                        "filter_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause applied to "
                                "filter_item_id to select which polygon "
                                "feature(s) to use as the filter. E.g. "
                                "\"DistrictName = 'District 5'\". All "
                                "matching features' rings are unioned."
                            ),
                            "default": "1=1",
                        },
                        "filter_geometry": {
                            "type": "object",
                            "description": (
                                "Inline GeoJSON Polygon, MultiPolygon, "
                                "or Feature wrapping one. Use this "
                                "instead of filter_item_id when you "
                                "already have the polygon in hand. "
                                "Coordinates must be WGS84 (EPSG:4326)."
                            ),
                        },
                        "spatial_rel": {
                            "type": "string",
                            "enum": [
                                "intersects",
                                "contains",
                                "within",
                                "crosses",
                                "touches",
                                "overlaps",
                                "envelope_intersects",
                            ],
                            "description": (
                                "ArcGIS spatial relation: 'intersects' "
                                "(any overlap, default), 'contains' "
                                "(filter contains target), 'within' "
                                "(target within filter), 'crosses', "
                                "'touches', 'overlaps', "
                                "'envelope_intersects'."
                            ),
                            "default": "intersects",
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE clause to further "
                                "filter target features by attribute."
                            ),
                            "default": "1=1",
                        },
                        "distance": {
                            "type": "number",
                            "description": (
                                "Proximity buffer: expand the filter "
                                "polygon by this distance (in `units`) "
                                "before testing the spatial relation. Use "
                                "for 'within N of' questions, e.g. "
                                "distance=0.5 units='miles' for 'within "
                                "half a mile of'. Omit or 0 for an exact "
                                "(unbuffered) overlap."
                            ),
                        },
                        "units": {
                            "type": "string",
                            "enum": [
                                "meters",
                                "kilometers",
                                "feet",
                                "miles",
                                "yards",
                            ],
                            "description": (
                                "Linear unit for `distance` (default "
                                "meters). Ignored when distance is "
                                "omitted."
                            ),
                            "default": "meters",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max features to return (default 25, "
                                "capped at 1000; capped at 50 when "
                                "return_geometry=true)."
                            ),
                            "default": 25,
                            "minimum": 1,
                            "maximum": 1000,
                        },
                        "return_geometry": {
                            "type": "boolean",
                            "description": (
                                "If true, each record includes a "
                                "GeoJSON geometry in WGS84, "
                                "server-side simplified to ~5m. "
                                "Useful for precise client-side "
                                "clipping. Limit is capped at 50."
                            ),
                            "default": False,
                        },
                    },
                    "required": ["item_id"],
                },
            ),
            ToolDefinition(
                name="aggregate_by_polygon",
                description=(
                    f"Bucket records from one {city} layer into "
                    f"polygons from another and return counts + sums "
                    f"per bucket. Answers 'how many X per community "
                    f"council', 'total tons by district', etc. "
                    f"Source can be points/polylines/polygons; "
                    f"aggregation must be polygons. Returns count + "
                    f"summed numeric fields per bucket, plus an "
                    f"unmatched count. Prefer this over a loop of "
                    f"spatial_query_point calls. PROXIMITY: set "
                    f"`buffer_distance` + `buffer_units` to bucket source "
                    f"features that fall WITHIN that distance of a polygon "
                    f"(not just inside it) -- e.g. population points "
                    f"within half a mile of each park: source=population, "
                    f"aggregation=parks, buffer_distance=0.5, "
                    f"buffer_units='miles'."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the layer whose "
                                "records you want to bucket (points, "
                                "polylines, or polygons)."
                            ),
                        },
                        "aggregation_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the polygon layer "
                                "to bucket into (e.g. community "
                                "councils, assembly districts)."
                            ),
                        },
                        "group_by_field": {
                            "type": "string",
                            "description": (
                                "Field on the aggregation layer whose "
                                "value labels each bucket (e.g. "
                                "'COUNCIL', 'DISTRICT_NAME')."
                            ),
                        },
                        "sum_fields": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Numeric field names from the source "
                                "layer to sum per bucket. Omit for "
                                "count-only."
                            ),
                        },
                        "count": {
                            "type": "boolean",
                            "description": (
                                "Include a feature count per bucket "
                                "(default true)."
                            ),
                            "default": True,
                        },
                        "source_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause applied to the source "
                                "layer before aggregating."
                            ),
                            "default": "1=1",
                        },
                        "agg_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause to narrow the "
                                "aggregation polygons (e.g. exclude "
                                "retired districts)."
                            ),
                            "default": "1=1",
                        },
                        "centroid_mode": {
                            "type": "string",
                            "enum": [
                                "auto",
                                "centroid",
                                "representative_point",
                            ],
                            "description": (
                                "How to reduce source features to a "
                                "single point for the join. For "
                                "polygons: 'centroid' is cheap, "
                                "'representative_point' stays inside "
                                "L-shaped/donut shapes, 'auto' (default) "
                                "uses centroid unless it falls outside. "
                                "For polylines (road centerlines, "
                                "trails, transit routes): 'auto' and "
                                "'representative_point' use the line "
                                "midpoint, interpolated at 50% along the "
                                "line's arc length -- always on the line "
                                "itself. 'centroid' uses the "
                                "length-weighted centroid of the line "
                                "(NOT a bounding-box centroid); on a "
                                "curved or U-shaped line that point can "
                                "fall off the line, so prefer 'auto' for "
                                "polyline sources. Note: a road segment "
                                "straddling two districts is assigned to "
                                "whichever district contains its "
                                "midpoint, not both -- use "
                                "spatial_query_polygon if you need "
                                "intersects-based assignment."
                            ),
                            "default": "auto",
                        },
                        "overlap_policy": {
                            "type": "string",
                            "enum": [
                                "first_match",
                                "all_matches",
                                "largest",
                            ],
                            "description": (
                                "How to handle source points that fall "
                                "inside overlapping aggregation "
                                "polygons. 'first_match' (default) is "
                                "fastest; 'all_matches' double-counts "
                                "but is honest; 'largest' picks the "
                                "biggest polygon deterministically."
                            ),
                            "default": "first_match",
                        },
                        "buffer_distance": {
                            "type": "number",
                            "description": (
                                "Proximity buffer: bucket a source feature "
                                "into a polygon when its point is inside OR "
                                "within this distance (in `buffer_units`) "
                                "of the polygon. Use for 'within N of' "
                                "questions, e.g. buffer_distance=0.5 "
                                "buffer_units='miles'. Omit or 0 for "
                                "strict containment."
                            ),
                        },
                        "buffer_units": {
                            "type": "string",
                            "enum": [
                                "meters",
                                "kilometers",
                                "feet",
                                "miles",
                                "yards",
                            ],
                            "description": (
                                "Linear unit for `buffer_distance` "
                                "(default meters). Ignored when "
                                "buffer_distance is omitted."
                            ),
                            "default": "meters",
                        },
                        "max_source_features": {
                            "type": "integer",
                            "description": (
                                "Safety cap on source features pulled "
                                "(default 5000, max 5000). Narrow "
                                "source_where if you hit the cap."
                            ),
                            "default": 5000,
                        },
                    },
                    "required": [
                        "source_item_id",
                        "aggregation_item_id",
                        "group_by_field",
                    ],
                },
            ),
            ToolDefinition(
                name="filter_by_polygon",
                description=(
                    f"Return the subset of records from one {city} layer "
                    f"that fall inside a named polygon (or polygons) "
                    f"from another layer. Use this for questions like "
                    f"'what are the reports in Fairview', 'show me the "
                    f"cleanups in Midtown'. The polygon is identified by "
                    f"SQL, not coordinates -- never ask the user for "
                    f"lat/lon when a polygon name will do. If "
                    f"container_where matches 0 polygons, returns a "
                    f"clear error (not a silently empty result). "
                    f"Multi-polygon containers (e.g. "
                    f"COUNCIL IN ('A','B','C')) are unioned."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the layer to filter."
                            ),
                        },
                        "container_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the polygon layer "
                                "that provides the named container "
                                "(e.g. a community-councils layer)."
                            ),
                        },
                        "container_where": {
                            "type": "string",
                            "description": (
                                "SQL to pick the container polygon(s), "
                                "e.g. \"COUNCIL='Fairview'\" or "
                                "\"COUNCIL IN ('Midtown','Fairview')\". "
                                "Must match >=1 polygon; 0 matches is "
                                "reported as an error."
                            ),
                        },
                        "source_where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE clause applied to "
                                "source records as well."
                            ),
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names to return."
                            ),
                            "default": "*",
                        },
                        "return_geometry": {
                            "type": "boolean",
                            "description": (
                                "If true, each record includes GeoJSON "
                                "geometry (WGS84, simplified). Limit "
                                "caps at 50 when true."
                            ),
                            "default": False,
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max features to return (default 100, "
                                "max 1000; 50 when return_geometry=true)."
                            ),
                            "default": 100,
                        },
                    },
                    "required": [
                        "source_item_id",
                        "container_item_id",
                        "container_where",
                    ],
                },
            ),
            ToolDefinition(
                name="find_features_spanning_classifications",
                description=(
                    f"Find {city} features that cross multiple values "
                    f"of a classification field -- the 'split-zoned "
                    f"parcels', 'parcels on a flood-zone boundary', "
                    f"'roads crossing district lines' pattern. "
                    f"`source_item_id` is the layer of THINGS to "
                    f"return (parcels, roads); "
                    f"`classification_item_id` is a polygon layer of "
                    f"REGIONS (zoning, flood). Use DIFFERENT layers "
                    f"for each. Returns a touch-count histogram + the "
                    f"qualifying features in one call -- no chaining "
                    f"needed. Pre-flight with `get_layer_schema` and "
                    f"`get_distinct_values` on the classification "
                    f"layer to confirm the field. Caps: source <= "
                    f"5000 matches, classification <= 1000 polygons."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "source_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the layer whose "
                                "features you want to test for "
                                "spanning (e.g. parcels, addresses, "
                                "roads). Can be polygon, polyline, "
                                "or point."
                            ),
                        },
                        "classification_item_id": {
                            "type": "string",
                            "description": (
                                "ArcGIS item ID of the polygon layer "
                                "whose distinct values define the "
                                "boundaries (e.g. zoning, flood "
                                "zones, community councils). MUST "
                                "be a polygon layer."
                            ),
                        },
                        "classification_field": {
                            "type": "string",
                            "description": (
                                "Field on the classification layer "
                                "whose distinct values matter "
                                "(e.g. 'ZONE_CODE', "
                                "'FLOOD_ZONE_TYPE', 'COUNCIL'). "
                                "CASE-SENSITIVE."
                            ),
                        },
                        "min_distinct": {
                            "type": "integer",
                            "description": (
                                "Minimum number of distinct "
                                "classification values a source "
                                "feature must touch to qualify. "
                                "Default 2 (= 'spans a boundary'). "
                                "Use 3+ for 'spans multiple "
                                "boundaries'."
                            ),
                            "default": 2,
                            "minimum": 2,
                        },
                        "source_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE on the source layer to "
                                "narrow features inspected. Required "
                                "to be tight enough that <= 5,000 "
                                "features match."
                            ),
                            "default": "1=1",
                        },
                        "classification_where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE on the classification "
                                "layer to narrow which polygons "
                                "contribute (e.g. exclude retired "
                                "districts)."
                            ),
                            "default": "1=1",
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated source-layer field "
                                "names to return for qualifying "
                                "features."
                            ),
                            "default": "*",
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                "Max qualifying features to list in "
                                "the response (default 100, max "
                                "1000). The histogram covers ALL "
                                "qualifying features regardless."
                            ),
                            "default": 100,
                        },
                        "max_source_features": {
                            "type": "integer",
                            "description": (
                                "Safety cap on source features "
                                "matching source_where (default "
                                "5000, max 5000). Tool refuses with "
                                "a clear error if exceeded."
                            ),
                            "default": 5000,
                        },
                    },
                    "required": [
                        "source_item_id",
                        "classification_item_id",
                        "classification_field",
                    ],
                },
            ),
        ]
        # Every tool in this plugin is read-only and reaches out to external
        # ArcGIS services, so advertise the MCP safety hints uniformly. This
        # lets clients (e.g. M365 Copilot) treat them as safe and skip
        # per-call confirmation prompts.
        for tool in tools:
            if tool.annotations is None:
                tool.annotations = {"readOnlyHint": True, "openWorldHint": True}
        return tools

    # ── Tool dispatch ─────────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "find_gis_content":
                text = await self._find_gis_content(arguments)

            elif tool_name == "browse_gallery":
                text = await self._browse_gallery(
                    arguments.get("keyword", "").strip(),
                    min(int(arguments.get("limit", 20)), 100),
                )

            elif tool_name == "search_spatial_layers":
                query = arguments.get("query", "").strip()
                if not query:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="query is required",
                    )
                text = await self._search_spatial_layers(
                    query,
                    arguments.get("layer_type", "all"),
                    min(int(arguments.get("limit", 10)), 50),
                )

            elif tool_name == "get_item_details":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                item = await self.get_dataset(item_id)
                text = self._format_details(item)

            elif tool_name == "get_layer_schema":
                text = await self._get_layer_schema(arguments)

            elif tool_name == "get_distinct_values":
                text = await self._get_distinct_values(arguments)

            elif tool_name == "find_parcel":
                text = await self._find_parcel(arguments)

            elif tool_name == "search_layers_by_field":
                text = await self._search_layers_by_field(arguments)

            elif tool_name == "query_data":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                where = arguments.get("where", "1=1")
                out_fields = arguments.get("out_fields", "*")
                order_by = arguments.get("order_by", "")
                date_format = arguments.get("date_format", "date")
                return_geometry = bool(
                    arguments.get("return_geometry", False)
                )
                requested_limit = int(arguments.get("limit", 25))
                effective_limit = (
                    min(requested_limit, self.GEOMETRY_LIMIT_CAP)
                    if return_geometry
                    else requested_limit
                )
                filters = {
                    "where": where,
                    "out_fields": out_fields,
                    "order_by": order_by,
                }

                # Resolve service URL once for parallel queries
                item = await self.get_dataset(item_id)
                service_url = self._ensure_layer_url(
                    item.get("url", "")
                )
                validated_where = WhereValidator.validate(where)

                # Fetch the layer quick-meta up front so we can
                # validate the WHERE clause against real field names
                # *before* incurring the query -- a typo'd field name
                # otherwise produces a cryptic ArcGIS error and a
                # round-trip wasted. Best-effort: if the meta fetch
                # fails (returns {}) we skip schema validation and
                # let the query speak for itself.
                quick_meta = await self._get_layer_quick_meta(service_url) or {}
                try:
                    WhereValidator.validate_against_schema(
                        validated_where, quick_meta.get("field_names")
                    )
                except ValueError as e:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message=str(e),
                    )

                # Now run the (more expensive) query and count in
                # parallel for latency.
                parallel_tasks = [
                    self.query_data(
                        item_id,
                        filters,
                        effective_limit,
                        return_geometry=return_geometry,
                    ),
                    self._get_record_count(service_url, validated_where),
                ]

                results = await asyncio.gather(*parallel_tasks)
                records = results[0]
                total_count = results[1]
                # When return_geometry=true the backend is f=geojson,
                # which renders dates as ISO strings already -- skip the
                # epoch-to-date conversion path.
                date_fields = (
                    quick_meta.get("date_fields")
                    if (date_format != "epoch" and not return_geometry)
                    else None
                )

                text = self._format_query_results(
                    records,
                    effective_limit,
                    total_count,
                    date_fields,
                    geometry_type=quick_meta.get("geometry_type"),
                    name_field=quick_meta.get("name_field"),
                    item_id=item_id,
                    service_url=service_url,
                    where=validated_where,
                    out_fields=out_fields,
                    coded_domains=(
                        quick_meta.get("coded_domains")
                        if not return_geometry
                        else None
                    ),
                    last_edit_date=quick_meta.get("last_edit_date"),
                    coverage_pct=quick_meta.get("coverage_pct"),
                )
                if not records:
                    text += self._no_data_hint(where)

            elif tool_name == "spatial_query_point":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                if "lon" not in arguments or "lat" not in arguments:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="lon and lat are required",
                    )
                limit = min(int(arguments.get("limit", 10)), 50)
                records, layer_meta = await asyncio.gather(
                    self.spatial_query_point(
                        item_id,
                        lon=arguments["lon"],
                        lat=arguments["lat"],
                        filters={
                            "where": arguments.get("where", "1=1"),
                            "out_fields": arguments.get("out_fields", "*"),
                        },
                        limit=limit,
                    ),
                    self._safe_layer_meta(item_id),
                )
                if not records:
                    text = (
                        f"No features in item `{item_id}` contain point "
                        f"(lon={arguments['lon']}, lat={arguments['lat']})."
                    )
                else:
                    # total_count=None avoids a misleading "of N total"
                    # line: every match is already in `records`, there
                    # is no paging going on. layer_meta is best-effort;
                    # raw rendering falls through on any failure.
                    text = self._format_query_results(
                        records,
                        limit,
                        total_count=None,
                        date_fields=layer_meta.get("date_fields"),
                        coded_domains=layer_meta.get("coded_domains"),
                        last_edit_date=layer_meta.get("last_edit_date"),
                        coverage_pct=layer_meta.get("coverage_pct"),
                    )

            elif tool_name == "spatial_query_polygon":
                item_id = arguments.get("item_id", "").strip()
                if not item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message="item_id is required",
                    )
                filter_geometry = arguments.get("filter_geometry")
                filter_item_id = (
                    arguments.get("filter_item_id") or ""
                ).strip() or None
                if not filter_geometry and not filter_item_id:
                    return ToolResult(
                        content=[],
                        success=False,
                        error_message=(
                            "spatial_query_polygon requires either "
                            "filter_geometry (inline GeoJSON) or "
                            "filter_item_id"
                        ),
                    )
                return_geometry = bool(
                    arguments.get("return_geometry", False)
                )
                requested_limit = int(arguments.get("limit", 25))
                effective_limit = (
                    min(requested_limit, self.GEOMETRY_LIMIT_CAP)
                    if return_geometry
                    else min(requested_limit, 1000)
                )
                records, layer_meta = await asyncio.gather(
                    self.spatial_query_polygon(
                        item_id,
                        filter_geometry=filter_geometry,
                        filter_item_id=filter_item_id,
                        filter_where=arguments.get("filter_where", "1=1"),
                        spatial_rel=arguments.get("spatial_rel", "intersects"),
                        filters={
                            "where": arguments.get("where", "1=1"),
                            "out_fields": arguments.get("out_fields", "*"),
                        },
                        limit=effective_limit,
                        return_geometry=return_geometry,
                        distance=arguments.get("distance"),
                        units=arguments.get("units", "meters"),
                    ),
                    self._safe_layer_meta(item_id),
                )
                # return_geometry=True uses f=geojson which renders
                # dates as ISO strings server-side, so skip our
                # epoch->ISO path. Also skip coded-domain decoding in
                # the GeoJSON branch since downstream tooling expects
                # the original codes.
                if return_geometry:
                    date_fields = None
                    coded_domains = None
                else:
                    date_fields = layer_meta.get("date_fields")
                    coded_domains = layer_meta.get("coded_domains")
                if not records:
                    text = (
                        f"No features in item `{item_id}` match the "
                        f"filter polygon."
                    )
                else:
                    text = self._format_query_results(
                        records,
                        effective_limit,
                        total_count=None,
                        date_fields=date_fields,
                        coded_domains=coded_domains,
                        last_edit_date=layer_meta.get("last_edit_date"),
                        coverage_pct=layer_meta.get("coverage_pct"),
                    )

            elif tool_name == "aggregate_by_polygon":
                text = await self._aggregate_by_polygon(arguments)

            elif tool_name == "filter_by_polygon":
                text = await self._filter_by_polygon(arguments)

            elif tool_name == "find_features_spanning_classifications":
                text = await self._find_features_spanning_classifications(
                    arguments
                )

            else:
                return ToolResult(
                    content=[],
                    success=False,
                    error_message=f"Unknown tool: {tool_name}",
                )

            return ToolResult(
                content=[
                    {"type": "text", "text": self._with_retrieved_footer(text)}
                ],
                success=True,
            )

        except Exception as e:
            logger.error(
                f"Error executing tool {tool_name}: {e}", exc_info=True
            )
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )

    # ── Health check ──────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(
                f"{self.plugin_config.portal_base_url}/search",
                params={
                    "q": f"orgid:{self.plugin_config.org_id}",
                    "f": "json",
                    "num": "1",
                },
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False
