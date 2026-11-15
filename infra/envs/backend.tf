# Estado remoto en S3 (bucket versionado creado por infra/bootstrap).
# Configuración parcial: los valores vienen de backends/<target>.s3.tfbackend
#   terraform init -backend-config=backends/floci.s3.tfbackend
#
# Con workspaces, cada ambiente guarda su estado en una clave distinta del MISMO bucket:
#   env:/dev/terraform.tfstate | env:/test/terraform.tfstate | env:/prod/terraform.tfstate
# Un apply en el workspace "test" solo puede leer/escribir env:/test/... — nunca el de prod.
terraform {
  backend "s3" {}
}
