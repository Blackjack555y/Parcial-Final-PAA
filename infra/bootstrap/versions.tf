terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
  }

  # El bootstrap crea el bucket donde vive TODO el estado del proyecto, incluido el suyo
  # (clave bootstrap/terraform.tfstate). Huevo y gallina: la primera vez, scripts/tf.sh aplica
  # con un backend local temporal (backend_override.tf) y migra el estado al bucket creado.
  backend "s3" {}
}
