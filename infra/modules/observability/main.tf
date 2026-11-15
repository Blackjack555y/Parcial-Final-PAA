# Logs centralizados por ambiente: /aegisvolt/<env>/<servicio>.
# Cada línea es JSON con: timestamp, level, env, service, version, correlation_id, saga_id, event.
#
# Metric filters → métricas del namespace AegisVolt/<env>, con dimensiones Service y Version.
# El canary (Fase 4) calcula la tasa de error de la versión nueva como:
#     HttpErrors{Version=nueva} / HttpRequests{Version=nueva}
# y solo promueve del 20 % al 100 % si es ≤ 5 % con un mínimo de peticiones.

locals {
  namespace = "AegisVolt/${var.env}"
}

resource "aws_cloudwatch_log_group" "service" {
  for_each = toset(var.services)

  name              = "/aegisvolt/${var.env}/${each.value}"
  retention_in_days = var.retention_days

  tags = { Service = each.value }
}

resource "aws_cloudwatch_log_metric_filter" "http_requests" {
  for_each = aws_cloudwatch_log_group.service

  name           = "http-requests"
  log_group_name = each.value.name
  pattern        = "{ $.event = \"http_request\" }"

  metric_transformation {
    name       = "HttpRequests"
    namespace  = local.namespace
    value      = "1"
    unit       = "Count"
    dimensions = { Service = "$.service", Version = "$.version" }
  }
}

resource "aws_cloudwatch_log_metric_filter" "http_errors" {
  for_each = aws_cloudwatch_log_group.service

  name           = "http-errors-5xx"
  log_group_name = each.value.name
  pattern        = "{ $.event = \"http_request\" && $.status >= 500 }"

  metric_transformation {
    name       = "HttpErrors"
    namespace  = local.namespace
    value      = "1"
    unit       = "Count"
    dimensions = { Service = "$.service", Version = "$.version" }
  }
}

resource "aws_cloudwatch_log_metric_filter" "saga_compensations" {
  for_each = { for k, v in aws_cloudwatch_log_group.service : k => v if k == "graphql-gateway" }

  name           = "saga-compensations"
  log_group_name = each.value.name
  pattern        = "{ $.event = \"saga_compensated\" }"

  metric_transformation {
    name      = "SagaCompensations"
    namespace = local.namespace
    value     = "1"
    unit      = "Count"
  }
}
