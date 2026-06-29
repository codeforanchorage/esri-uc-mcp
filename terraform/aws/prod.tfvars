lambda_name = "anchorage-ecode-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB is ample: the eCode plugin is a thin async HTTP proxy to the eCode360
# API (no in-memory geometry work), so it needs little memory or CPU.
lambda_memory   = 512
lambda_timeout  = 120
api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10
custom_domain   = "anchorage-ecode.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Conversational MCP traffic does
# not need horizontal scale; raise if legitimate users start getting throttled.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
waf_rate_limit_per_5min = 300

# Hardened, API-key-gated /mcp-gcc route for an M365 GCC Copilot consumer.
# Kept enabled. The Copilot Studio connector isn't wired up yet, but the route
# + API key are live in prod and retained for when it is. (The buffering tools
# + instructions also ship on the public /mcp route, same Lambda.) Retrieve the
# key with: terraform output -raw gcc_api_key_value
enable_gcc_route = true
