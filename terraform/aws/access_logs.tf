# API Gateway access logs — structured JSON to CloudWatch for per-request
# analytics (distinct users, tool usage, latency).
#
# The aws_api_gateway_account resource below is a REGION-level singleton:
# AWS stores one CloudWatch role ARN per region, and applying this will
# overwrite whatever is there. If another stack in the same account/region
# already manages it, either import it (`terraform import
# aws_api_gateway_account.this api-gateway-account`) or comment these
# resources out.

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
}

resource "aws_cloudwatch_log_group" "api_gateway_access" {
  name              = "/aws/apigateway/${local.lambda_name}-access"
  retention_in_days = 30

  lifecycle {
    create_before_destroy = true
  }
}
