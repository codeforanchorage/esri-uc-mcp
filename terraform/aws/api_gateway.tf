# API Gateway REST API
resource "aws_api_gateway_rest_api" "mcp_api" {
  name        = "${local.lambda_name}-api"
  description = "API Gateway for OpenContext MCP Server"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

# API Gateway Resource: /mcp
resource "aws_api_gateway_resource" "mcp" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_rest_api.mcp_api.root_resource_id
  path_part   = "mcp"
}

# API Gateway Method: POST
resource "aws_api_gateway_method" "mcp_post" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = false
}

# API Gateway Method: GET (MCP Streamable HTTP spec allows GET for SSE;
# Lambda returns 405 Method Not Allowed, which is spec-compliant)
resource "aws_api_gateway_method" "mcp_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false
}

# API Gateway Method: DELETE (MCP spec session termination;
# Lambda returns 405 Method Not Allowed)
resource "aws_api_gateway_method" "mcp_delete" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "DELETE"
  authorization    = "NONE"
  api_key_required = false
}

# API Gateway Method: OPTIONS (for CORS, no API key required)
resource "aws_api_gateway_method" "mcp_options" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "OPTIONS"
  authorization    = "NONE"
  api_key_required = false
}

# Lambda Integration for POST
resource "aws_api_gateway_integration" "mcp_post_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_post.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# Lambda Integration for GET
resource "aws_api_gateway_integration" "mcp_get_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_get.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# Lambda Integration for DELETE
resource "aws_api_gateway_integration" "mcp_delete_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_delete.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# Lambda Integration for OPTIONS (preflight handled by Lambda so the
# Origin allowlist enforced in server/http_handler.py:_get_cors_headers
# applies to preflight as well as POST. Previously this was a MOCK
# integration that returned hard-coded `Access-Control-Allow-Origin: *`,
# which made the Lambda-level allowlist useless for browser clients —
# preflight is what the browser checks first.)
resource "aws_api_gateway_integration" "mcp_options_integration" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_options.http_method

  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# Method Response for POST. With AWS_PROXY integrations the headers in
# the Lambda response pass through directly, so this resource is only
# documenting which CORS headers can appear; it does not enforce values.
resource "aws_api_gateway_method_response" "mcp_post_response_200" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_post.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
  }
}

# Lambda Permission for API Gateway
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp_server.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.mcp_api.execution_arn}/*/*"
}

# API Gateway Deployment
resource "aws_api_gateway_deployment" "mcp_deployment" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.mcp.id,
      aws_api_gateway_method.mcp_post.id,
      aws_api_gateway_method.mcp_get.id,
      aws_api_gateway_method.mcp_delete.id,
      aws_api_gateway_method.mcp_options.id,
      aws_api_gateway_integration.mcp_post_integration.id,
      aws_api_gateway_integration.mcp_get_integration.id,
      aws_api_gateway_integration.mcp_delete_integration.id,
      aws_api_gateway_integration.mcp_options_integration.id,
      # Hardened GCC route (empty list when var.enable_gcc_route = false).
      aws_api_gateway_resource.mcp_gcc[*].id,
      aws_api_gateway_method.mcp_gcc_post[*].id,
      aws_api_gateway_integration.mcp_gcc_post_integration[*].id,
      # Lambda ARN: a Lambda rename updates integration URI in place; without
      # this, the deployment snapshot keeps pointing at the old function.
      aws_lambda_function.mcp_server.arn,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_method.mcp_post,
    aws_api_gateway_method.mcp_get,
    aws_api_gateway_method.mcp_delete,
    aws_api_gateway_method.mcp_options,
    aws_api_gateway_integration.mcp_post_integration,
    aws_api_gateway_integration.mcp_get_integration,
    aws_api_gateway_integration.mcp_delete_integration,
    aws_api_gateway_integration.mcp_options_integration,
    aws_api_gateway_method_response.mcp_post_response_200,
    # Hardened GCC route (no-ops when var.enable_gcc_route = false).
    aws_api_gateway_method.mcp_gcc_post,
    aws_api_gateway_integration.mcp_gcc_post_integration,
  ]
}

# API Gateway Stage
resource "aws_api_gateway_stage" "prod" {
  deployment_id = aws_api_gateway_deployment.mcp_deployment.id
  rest_api_id   = aws_api_gateway_rest_api.mcp_api.id
  stage_name    = var.stage_name

  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway_access.arn
    format = jsonencode({
      requestTime        = "$context.requestTime"
      requestId          = "$context.requestId"
      httpMethod         = "$context.httpMethod"
      resourcePath       = "$context.resourcePath"
      status             = "$context.status"
      sourceIp           = "$context.identity.sourceIp"
      userAgent          = "$context.identity.userAgent"
      integrationLatency = "$context.integrationLatency"
      responseLength     = "$context.responseLength"
    })
  }

  depends_on = [aws_api_gateway_account.this]

  # Rename-safety: base-path mapping repoints to new stage before old is destroyed.
  lifecycle {
    create_before_destroy = true
  }
}

# Method Settings: Throttling for all methods in stage (AWS format: */* not /*/*)
resource "aws_api_gateway_method_settings" "mcp_post" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}

# Usage Plan
resource "aws_api_gateway_usage_plan" "mcp_usage_plan" {
  name = "${local.lambda_name}-usage-plan"

  api_stages {
    api_id = aws_api_gateway_rest_api.mcp_api.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }

  quota_settings {
    limit  = var.api_quota_limit
    period = "DAY"
  }

  throttle_settings {
    burst_limit = var.api_burst_limit
    rate_limit  = var.api_rate_limit
  }
}

# ── Hardened GCC route: /mcp-gcc ─────────────────────────────────────────
# A second MCP route on the SAME Lambda for the M365 GCC Copilot consumer.
# Unlike the public /mcp route, this one REQUIRES an API key, enforced at the
# gateway before the Lambda is invoked. The handler already accepts the
# /mcp-gcc path (server/http_handler.py MCP_PATHS), so auth here is purely an
# API Gateway concern. Gated by var.enable_gcc_route so other forks /
# deployments that share this codebase don't create the route.

resource "aws_api_gateway_resource" "mcp_gcc" {
  count       = var.enable_gcc_route ? 1 : 0
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_rest_api.mcp_api.root_resource_id
  path_part   = "mcp-gcc"
}

# POST only: no OPTIONS/CORS, since the Copilot connector calls server-side
# (no browser preflight). authorization=NONE + api_key_required=true means
# "valid x-api-key required, no IAM/JWT" -- keyless calls get 403 at the gateway.
resource "aws_api_gateway_method" "mcp_gcc_post" {
  count            = var.enable_gcc_route ? 1 : 0
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp_gcc[0].id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "mcp_gcc_post_integration" {
  count                   = var.enable_gcc_route ? 1 : 0
  rest_api_id             = aws_api_gateway_rest_api.mcp_api.id
  resource_id             = aws_api_gateway_resource.mcp_gcc[0].id
  http_method             = aws_api_gateway_method.mcp_gcc_post[0].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# API key for the Copilot/GCC connector. The value is generated by AWS;
# retrieve with `terraform output -raw gcc_api_key_value` and store it as a
# SHARED connection on the Power Platform custom connector so end users never
# see it. Rotate by tainting this resource.
resource "aws_api_gateway_api_key" "gcc" {
  count = var.enable_gcc_route ? 1 : 0
  name  = "${local.lambda_name}-gcc-key"
}

# Associate the key with the existing usage plan (which already covers the
# prod stage). This makes the key valid for the keyed method and meters its
# usage separately from the unkeyed /mcp (Claude) traffic, which is not
# counted against any usage plan.
resource "aws_api_gateway_usage_plan_key" "gcc" {
  count         = var.enable_gcc_route ? 1 : 0
  key_id        = aws_api_gateway_api_key.gcc[0].id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.mcp_usage_plan.id
}
