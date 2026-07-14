lambda_name = "esri-uc-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB is ample: the esri_uc plugin serves a bundled ~4 MB JSON snapshot
# entirely from memory — no upstream calls, no geometry work. This is the
# cheapest server in the fleet; cold starts are fast because there is no
# connectivity check at initialize().
lambda_memory   = 512
lambda_timeout  = 120
api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10
# DNS for codeforanchorage.org is managed externally at DreamHost, so the
# ACM validation CNAME and the routing CNAME are added there by hand (see
# outputs acm_validation_cname_name/value and custom_domain_target).
custom_domain = "esri-uc.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Conversational MCP traffic does
# not need horizontal scale; raise if legitimate users start getting throttled.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
waf_rate_limit_per_5min = 300

# Only the public /mcp route is served; the API-key-gated /mcp-gcc route is off.
enable_gcc_route = false
