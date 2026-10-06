terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# Credentials come from the usual chain (AWS_PROFILE, `aws sso login`, ...).
provider "aws" {
  region = var.region

  default_tags {
    tags = merge({ app = "annotide" }, var.tags)
  }
}
