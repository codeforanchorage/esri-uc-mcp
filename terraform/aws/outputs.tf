output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.mcp_server.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.mcp_server.arn
}

output "cloudwatch_log_group" {
  description = "CloudWatch Log Group name"
  value       = aws_cloudwatch_log_group.lambda_logs.name
}

output "api_gateway_access_log_group" {
  description = "CloudWatch Log Group for API Gateway access logs (distinct-users analysis)"
  value       = aws_cloudwatch_log_group.api_gateway_access.name
}

output "api_gateway_url" {
  description = "API Gateway URL for MCP server (public production endpoint)"
  value       = "${aws_api_gateway_stage.prod.invoke_url}/mcp"
}

# ── Custom domain outputs (only populated when var.custom_domain is set) ────

output "acm_certificate_arn" {
  description = "ARN of the ACM certificate for the custom domain"
  value       = var.custom_domain != "" ? aws_acm_certificate.mcp_cert[0].arn : null
}

output "acm_validation_cname_name" {
  description = "DNS CNAME record name that city IT must create for ACM validation"
  value       = var.custom_domain != "" ? tolist(aws_acm_certificate.mcp_cert[0].domain_validation_options)[0].resource_record_name : null
}

output "acm_validation_cname_value" {
  description = "DNS CNAME record value for ACM validation"
  value       = var.custom_domain != "" ? tolist(aws_acm_certificate.mcp_cert[0].domain_validation_options)[0].resource_record_value : null
}

output "custom_domain_target" {
  description = "API Gateway regional domain name — city IT points the custom domain CNAME here"
  value       = var.custom_domain != "" ? aws_api_gateway_domain_name.custom[0].regional_domain_name : null
}

# ── GCC route outputs (only populated when var.enable_gcc_route = true) ─────

output "gcc_mcp_url" {
  description = "API-key-gated MCP URL for the M365 GCC Copilot connector"
  value       = var.enable_gcc_route ? "${aws_api_gateway_stage.prod.invoke_url}/mcp-gcc" : null
}

output "gcc_api_key_id" {
  description = "ID of the API key for the GCC Copilot connector"
  value       = var.enable_gcc_route ? aws_api_gateway_api_key.gcc[0].id : null
}

output "gcc_api_key_value" {
  description = "Secret API key for the GCC Copilot connector — configure as a shared connection on the Power Platform custom connector (end users never see it)"
  value       = var.enable_gcc_route ? aws_api_gateway_api_key.gcc[0].value : null
  sensitive   = true
}
