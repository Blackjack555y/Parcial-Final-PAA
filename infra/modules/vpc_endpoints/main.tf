# VPC Endpoints: el tráfico de los pods hacia AWS nunca sale a internet.
#
#  Gateway (S3, DynamoDB): sin costo; agregan a las tablas de rutas privadas una ruta cuyo
#  destino es la prefix list del servicio. La política del endpoint limita QUÉ recursos se
#  pueden usar a través de él: solo las tablas de ESTE ambiente.
#
#  Interface (execute-api, logs, monitoring): una ENI con IP privada por subred; el DNS
#  privado hace que logs.<region>.amazonaws.com resuelva a esa IP dentro de la VPC.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  table_arns = "arn:aws:dynamodb:${var.aws_region}:${local.account_id}:table/aegisvolt-*-${var.env}"
}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.private_route_table_ids

  # Aislamiento a nivel de red: desde la VPC de "test" no se puede tocar una tabla "-prod".
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyThisEnvironmentTables"
      Effect    = "Allow"
      Principal = "*"
      Action    = "dynamodb:*"
      Resource  = [local.table_arns, "${local.table_arns}/index/*"]
    }]
  })

  tags = { Name = "${var.name}-vpce-dynamodb" }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = var.private_route_table_ids

  tags = { Name = "${var.name}-vpce-s3" }
}

resource "aws_security_group" "interface_endpoints" {
  name        = "${var.name}-vpce-interface"
  description = "HTTPS desde la VPC hacia los Interface Endpoints"
  vpc_id      = var.vpc_id

  tags = { Name = "${var.name}-vpce-interface" }
}

resource "aws_vpc_security_group_ingress_rule" "https_from_vpc" {
  security_group_id = aws_security_group.interface_endpoints.id
  description       = "HTTPS solo desde dentro de la VPC"
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_endpoint" "interface" {
  for_each = toset(var.interface_services)

  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.interface_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${var.name}-vpce-${each.value}" }
}
