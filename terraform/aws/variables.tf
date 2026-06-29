variable "lambda_name" {
  description = "Name of the Lambda function"
  type        = string
  default     = "opencontext-mcp-server"
}

variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

variable "config_file" {
  description = "Path to config.yaml file"
  type        = string
  default     = "../../config.yaml"
}

variable "lambda_memory" {
  description = "Lambda memory in MB"
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 120
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions cap for the Lambda. -1 means unreserved (default account limit). Set to a small integer (e.g. 10) to bound cost and blast radius from traffic spikes."
  type        = number
  default     = 10
}

variable "api_quota_limit" {
  description = "API Gateway daily request quota"
  type        = number
  default     = 1000
}

variable "api_rate_limit" {
  description = "API Gateway requests per second rate limit"
  type        = number
  default     = 5
}

variable "api_burst_limit" {
  description = "API Gateway burst limit"
  type        = number
  default     = 10
}

variable "stage_name" {
  description = "API Gateway stage name (e.g. prod, dev, staging)"
  type        = string
  default     = "staging"
}

variable "custom_domain" {
  description = "Custom domain name for API Gateway (leave empty to skip custom domain setup)"
  type        = string
  default     = ""
}

variable "waf_rate_limit_per_5min" {
  description = "WAF per-IP rate limit over a 5-minute window. Set to 0 to disable WAF entirely."
  type        = number
  default     = 1000
}

variable "alarm_sns_topic_arn" {
  description = "Optional SNS topic ARN to notify on CloudWatch alarm state changes. Leave empty to create alarms without notification actions."
  type        = string
  default     = ""
}

variable "enable_gcc_route" {
  description = "Create the hardened, API-key-required /mcp-gcc route (for the M365 GCC Copilot consumer) on the same Lambda. Leave false on deployments that only serve the public /mcp route."
  type        = bool
  default     = false
}

variable "ecode_api_key" {
  description = "eCode360 API key (the 'api-key' header). Provide via a gitignored *.auto.tfvars file or TF_VAR_ecode_api_key; never commit it."
  type        = string
  sensitive   = true
  default     = ""
}

variable "ecode_api_secret" {
  description = "eCode360 API secret (the 'api-secret' header). Provide via a gitignored *.auto.tfvars file or TF_VAR_ecode_api_secret; never commit it."
  type        = string
  sensitive   = true
  default     = ""
}
