###############################################################################
# AWS WAFv2 web ACL for the MCP API Gateway stage.
#
# Provides per-IP rate limiting and AWS-managed attack signature rules in
# front of the public /mcp endpoint. The endpoint itself has no auth, so
# this is the primary line of defense against abuse (denial-of-wallet,
# scrapers, known exploit payloads).
#
# Set `waf_rate_limit_per_5min = 0` to skip creating the WAF entirely.
###############################################################################

locals {
  waf_enabled = var.waf_rate_limit_per_5min > 0
}

resource "aws_wafv2_web_acl" "mcp_api" {
  count = local.waf_enabled ? 1 : 0

  name  = "${local.lambda_name}-waf"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # Per-IP rate limit. Blocks any IP that sends more than
  # `waf_rate_limit_per_5min` requests in a rolling 5-minute window.
  rule {
    name     = "RateLimitPerIP"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit_per_5min
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.lambda_name}-RateLimitPerIP"
      sampled_requests_enabled   = true
    }
  }

  # Known bad inputs (log4shell, exploit strings, generic probe patterns).
  # Safe to run in Block mode for a JSON-RPC API.
  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.lambda_name}-KnownBadInputs"
      sampled_requests_enabled   = true
    }
  }

  # Common rule set in Count mode for the first rollout — some rules
  # (SizeRestrictions_BODY, NoUserAgent_HEADER) can false-positive on
  # JSON-RPC clients. Observe metrics for a few days, then flip
  # override_action to `none {}` to enforce.
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 3

    override_action {
      count {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.lambda_name}-CommonRuleSet"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.lambda_name}-waf"
    sampled_requests_enabled   = true
  }

  # Rename-safety: association repoints to new WAF before old is destroyed.
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_wafv2_web_acl_association" "mcp_api" {
  count = local.waf_enabled ? 1 : 0

  resource_arn = aws_api_gateway_stage.prod.arn
  web_acl_arn  = aws_wafv2_web_acl.mcp_api[0].arn
}
