terraform {
  backend "s3" {
    bucket         = "anchorage-gis-opencontext-tfstate"
    key            = "terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}
