# API Gateway access logs — structured JSON to CloudWatch for per-request
# analytics (distinct users, tool usage, latency).
#
# aws_api_gateway_account is a REGION-level singleton: AWS stores one
# CloudWatch role ARN per account+region. When another stack in the same
# region also manages it (here: ebird-mcp also in us-west-2), both stacks
# fight over the cloudwatch_role_arn on every apply. We resolve this by
# still creating the role for this stack but telling Terraform to leave
# the account-level ARN alone after initial set — whichever stack
# touched it last wins, and subsequent deploys don't churn it.

resource "aws_iam_role" "api_gateway_cloudwatch" {
  name = "${local.lambda_name}-apigw-cloudwatch"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "apigateway.amazonaws.com"
        }
      }
    ]
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch" {
  role       = aws_iam_role.api_gateway_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "this" {
  cloudwatch_role_arn = aws_iam_role.api_gateway_cloudwatch.arn

  lifecycle {
    ignore_changes = [cloudwatch_role_arn]
  }
}

resource "aws_cloudwatch_log_group" "api_gateway_access" {
  name              = "/aws/apigateway/${local.lambda_name}-access"
  retention_in_days = 30

  lifecycle {
    create_before_destroy = true
  }
}
