# Database-per-Service: cada microservicio es dueño exclusivo de su tabla; nadie más la lee
# ni la escribe (la integración entre servicios es por API, nunca por la base de datos).
#
#   reservations → station-reservation-service   (conectores y bloqueos)
#   billing      → billing-wallet-service        (billeteras y transacciones)
#   dispatch     → energy-dispatch-service       (sesiones de carga y telemetría)
#   sagas        → graphql-gateway (orquestador) (bitácora de la SAGA)
#   flags        → graphql-gateway               (feature flags del ambiente)
#
# Todas usan el ambiente en el nombre: aegisvolt-<tabla>-<env>.

locals {
  tables = {
    reservations = { range_key = "sk", ttl = false, gsi = false }
    billing      = { range_key = "sk", ttl = false, gsi = false }
    dispatch     = { range_key = "sk", ttl = true, gsi = false } # telemetría con expiración
    sagas        = { range_key = "sk", ttl = false, gsi = true } # GSI por estado (auditoría)
    flags        = { range_key = null, ttl = false, gsi = false }
  }
}

resource "aws_dynamodb_table" "this" {
  for_each = local.tables

  name                        = "aegisvolt-${each.key}-${var.env}"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = each.value.range_key
  deletion_protection_enabled = var.deletion_protection

  attribute {
    name = "pk"
    type = "S"
  }

  dynamic "attribute" {
    for_each = each.value.range_key == null ? [] : [each.value.range_key]
    content {
      name = attribute.value
      type = "S"
    }
  }

  # sagas: consultar "todas las sagas en estado X ordenadas por fecha" sin Scan (Prefect).
  dynamic "attribute" {
    for_each = each.value.gsi ? ["gsi1pk", "gsi1sk"] : []
    content {
      name = attribute.value
      type = "S"
    }
  }

  dynamic "global_secondary_index" {
    for_each = each.value.gsi ? [1] : []
    content {
      name            = "by-status"
      projection_type = "ALL"

      key_schema {
        attribute_name = "gsi1pk"
        key_type       = "HASH"
      }

      key_schema {
        attribute_name = "gsi1sk"
        key_type       = "RANGE"
      }
    }
  }

  dynamic "ttl" {
    for_each = each.value.ttl ? [1] : []
    content {
      attribute_name = "expires_at"
      enabled        = true
    }
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Name = "aegisvolt-${each.key}-${var.env}", Owner = each.key }
}

# Siembra de flags: Terraform crea el valor inicial y luego LO IGNORA (ignore_changes).
# Así un cambio de flag en caliente (mutación GraphQL / consola) no se revierte en el próximo
# apply, y cada ambiente conserva su propio estado de flags.
resource "aws_dynamodb_table_item" "flag" {
  for_each = var.feature_flags

  table_name = aws_dynamodb_table.this["flags"].name
  hash_key   = "pk"

  item = jsonencode({
    pk          = { S = each.key }
    enabled     = { BOOL = each.value.enabled }
    description = { S = each.value.description }
    updated_by  = { S = "terraform" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}
