terraform {
  required_version = ">= 1.10" # 1.10+: bloqueo nativo del estado en S3 (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
  }
}
