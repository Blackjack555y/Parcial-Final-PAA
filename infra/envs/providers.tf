provider "aws" {
  region = var.aws_region

  # Floci: credenciales ficticias y todos los servicios en un único endpoint local.
  # AWS real: todos los valores en null → comportamiento estándar del provider.
  access_key                  = local.floci ? "test" : null
  secret_key                  = local.floci ? "test" : null
  skip_credentials_validation = local.floci
  skip_requesting_account_id  = local.floci
  skip_metadata_api_check     = local.floci
  s3_use_path_style           = local.floci

  endpoints {
    apigateway = local.floci ? var.floci_endpoint : null
    cloudwatch = local.floci ? var.floci_endpoint : null
    dynamodb   = local.floci ? var.floci_endpoint : null
    ec2        = local.floci ? var.floci_endpoint : null
    eks        = local.floci ? var.floci_endpoint : null
    elbv2      = local.floci ? var.floci_endpoint : null
    iam        = local.floci ? var.floci_endpoint : null
    logs       = local.floci ? var.floci_endpoint : null
    s3         = local.floci ? var.floci_endpoint : null
    sts        = local.floci ? var.floci_endpoint : null
  }

  default_tags {
    tags = {
      Project     = "aegisvolt"
      Environment = local.env
      ManagedBy   = "terraform"
    }
  }
}
