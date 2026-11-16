# Dos APIs REST por ambiente, ambas con el mismo backend privado:
#
#   public   (REGIONAL) → entrada de los clientes (frontend, HTTPS)
#   internal (PRIVATE)  → solo invocable desde la VPC a través del VPCE execute-api
#
#   Cliente ─HTTPS─► API Gateway ─VPC Link─► NLB interno ─► Ingress K8s ─► graphql-gateway
#
# El VPC Link es una ENI administrada por AWS dentro de la VPC: API Gateway entra a la red
# privada sin que el NLB ni los pods tengan IP pública.

locals {
  apis = {
    public   = "REGIONAL"
    internal = "PRIVATE"
  }
}

resource "aws_api_gateway_vpc_link" "this" {
  name        = "${var.name}-vpc-link"
  target_arns = [var.nlb_arn]
}

resource "aws_api_gateway_rest_api" "this" {
  for_each = local.apis

  name        = "${var.name}-${each.key}"
  description = "AegisVolt ${var.env} (${each.key})"

  endpoint_configuration {
    types            = [each.value]
    vpc_endpoint_ids = each.value == "PRIVATE" ? [var.execute_api_vpce_id] : null
  }
}

# La API interna rechaza cualquier llamada que no entre por el VPCE de este ambiente.
resource "aws_api_gateway_rest_api_policy" "internal" {
  rest_api_id = aws_api_gateway_rest_api.this["internal"].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.this["internal"].execution_arn}/*"
      },
      {
        Effect    = "Deny"
        Principal = "*"
        Action    = "execute-api:Invoke"
        Resource  = "${aws_api_gateway_rest_api.this["internal"].execution_arn}/*"
        Condition = { StringNotEquals = { "aws:SourceVpce" = var.execute_api_vpce_id } }
      },
    ]
  })
}

# /{proxy+} → todo el tráfico al Ingress (GraphQL en /graphql, health en /healthz).
resource "aws_api_gateway_resource" "proxy" {
  for_each = aws_api_gateway_rest_api.this

  rest_api_id = each.value.id
  parent_id   = each.value.root_resource_id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "proxy" {
  for_each = aws_api_gateway_resource.proxy

  rest_api_id   = each.value.rest_api_id
  resource_id   = each.value.id
  http_method   = "ANY"
  authorization = "NONE" # la autorización de mutaciones sensibles vive en la capa GraphQL

  request_parameters = { "method.request.path.proxy" = true }
}

resource "aws_api_gateway_integration" "proxy" {
  for_each = aws_api_gateway_method.proxy

  rest_api_id             = each.value.rest_api_id
  resource_id             = each.value.resource_id
  http_method             = each.value.http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "ANY"
  timeout_milliseconds    = 29000

  # VPC_LINK: el tráfico va SIEMPRE al NLB interno del VPC Link. El hostname de la URI no decide
  # el destino: es el Host que recibe el Ingress (api.<env>.aegisvolt.local) para enrutar al
  # ambiente correcto. En Floci, ese hostname se resuelve a minikube (extra_hosts en compose).
  uri             = "http://${var.backend_host}/{proxy}"
  connection_type = "VPC_LINK"
  connection_id   = aws_api_gateway_vpc_link.this.id

  request_parameters = { "integration.request.path.proxy" = "method.request.path.proxy" }
}

resource "aws_api_gateway_deployment" "this" {
  for_each = aws_api_gateway_rest_api.this

  rest_api_id = each.value.id

  # Redesplegar cuando cambie la integración o la política.
  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_integration.proxy[each.key].uri,
      aws_api_gateway_integration.proxy[each.key].connection_id,
      each.key == "internal" ? aws_api_gateway_rest_api_policy.internal.policy : "",
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [aws_api_gateway_integration.proxy]
}

resource "aws_api_gateway_stage" "this" {
  for_each = aws_api_gateway_deployment.this

  rest_api_id   = each.value.rest_api_id
  deployment_id = each.value.id
  stage_name    = var.env
}

# Límite de peticiones por segundo (protección básica contra abuso).
resource "aws_api_gateway_method_settings" "all" {
  for_each = aws_api_gateway_stage.this

  rest_api_id = each.value.rest_api_id
  stage_name  = each.value.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = var.throttle_rate_limit
    throttling_burst_limit = var.throttle_burst_limit
    metrics_enabled        = true
  }
}
