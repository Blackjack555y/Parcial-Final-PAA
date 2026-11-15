locals {
  # El ambiente ES el workspace. No hay variable "env": así es imposible aplicar la
  # configuración de prod estando en el workspace de test (o viceversa).
  env  = terraform.workspace
  name = "aegisvolt-${local.env}"

  allowed_envs = ["dev", "test", "prod"]
  is_prod      = local.env == "prod"
  floci        = var.target == "floci"

  services = [
    "graphql-gateway",
    "station-reservation-service",
    "billing-wallet-service",
    "energy-dispatch-service",
  ]
}

# Guardia: el plan falla en el workspace "default" o en cualquier nombre fuera de dev/test/prod,
# antes de crear un solo recurso.
resource "terraform_data" "workspace_guard" {
  input = local.env

  lifecycle {
    precondition {
      condition     = contains(local.allowed_envs, terraform.workspace)
      error_message = "Workspace \"${terraform.workspace}\" no permitido. Usa: terraform workspace select dev|test|prod"
    }
  }
}
