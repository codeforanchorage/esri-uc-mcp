lambda_name     = "anchorage-gis-mcp-prod"
stage_name      = "prod"
aws_region      = "us-west-2"
config_file     = "config.yaml"
# 1024 MB: aggregate_by_polygon holds up to 5000 source features in
# memory plus a bounded 32-entry polygon cache. Also buys more Lambda
# CPU, which accelerates the pure-Python point-in-polygon work.
lambda_memory   = 1024
lambda_timeout  = 120
api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10
custom_domain   = "anchorage-gis.codeforanchorage.org"
