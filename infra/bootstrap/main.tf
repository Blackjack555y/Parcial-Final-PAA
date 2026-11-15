locals {
  floci = var.target == "floci"
}

provider "aws" {
  region = var.aws_region

  # Con Floci: credenciales ficticias y endpoints locales. Con AWS real: todo en null y el
  # provider usa la cadena de credenciales normal (perfil, SSO u OIDC en CI).
  access_key                  = local.floci ? "test" : null
  secret_key                  = local.floci ? "test" : null
  skip_credentials_validation = local.floci
  skip_requesting_account_id  = local.floci
  skip_metadata_api_check     = local.floci
  s3_use_path_style           = local.floci

  endpoints {
    s3  = local.floci ? var.floci_endpoint : null
    sts = local.floci ? var.floci_endpoint : null
  }

  default_tags {
    tags = {
      Project   = "aegisvolt"
      Component = "terraform-state"
      ManagedBy = "terraform"
    }
  }
}

# Bucket único del estado. Cada workspace escribe en su propia clave:
#   env:/dev/terraform.tfstate   env:/test/terraform.tfstate   env:/prod/terraform.tfstate
resource "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name

  lifecycle {
    prevent_destroy = true # borrar este bucket borraría el estado de los tres ambientes
  }
}

# Versionamiento: cada `terraform apply` deja una versión nueva del tfstate y las anteriores
# quedan recuperables por VersionId (respaldo ante corrupción y auditoría).
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# El tfstate contiene datos sensibles: nunca público.
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Solo HTTPS (en AWS real; Floci no aplica políticas de transporte).
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}

# Las versiones anteriores se conservan N días; así el historial no crece sin límite.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}
