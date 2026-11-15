# NLB INTERNO delante del Ingress de Kubernetes. No tiene IP pública: solo se llega a él
#   1) desde API Gateway, a través del VPC Link, y
#   2) desde otras VPC/cuentas, a través del VPC Endpoint Service (PrivateLink) de abajo.
#
# En EKS real, el AWS Load Balancer Controller registra los pods del Ingress Controller en el
# target group (TargetGroupBinding, target_type = ip).

resource "aws_lb" "ingress" {
  name               = "${var.name}-ingress"
  load_balancer_type = "network"
  internal           = true
  subnets            = var.private_subnet_ids

  enable_cross_zone_load_balancing = true
}

resource "aws_lb_target_group" "ingress" {
  name        = "${var.name}-ingress"
  port        = var.ingress_port
  protocol    = "TCP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  # Salud por HTTP (no solo TCP): un pod que acepta conexiones pero responde 5xx sale de rotación.
  health_check {
    protocol            = "HTTP"
    path                = "/healthz"
    matcher             = "200-399"
    healthy_threshold   = 2
    unhealthy_threshold = 2
    interval            = 10
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.ingress.arn
  port              = 80
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.ingress.arn
  }
}

# PrivateLink: publica el Ingress del clúster como un servicio de VPC Endpoint.
# Floci 2.4 no implementa CreateVpcEndpointServiceConfiguration → enable_privatelink = false
# en floci.tfvars; en AWS real se crea (aws.tfvars).
data "aws_caller_identity" "current" {}

resource "aws_vpc_endpoint_service" "ingress" {
  count = var.enable_privatelink ? 1 : 0

  acceptance_required        = false
  network_load_balancer_arns = [aws_lb.ingress.arn]
  allowed_principals         = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]

  tags = { Name = "${var.name}-ingress-endpoint-service" }
}

# Consumidor del servicio dentro de la misma VPC: los clientes internos (p. ej. Prefect)
# llegan a los servicios de Kubernetes por esta ENI privada, sin pasar por internet.
resource "aws_security_group" "ingress_consumer" {
  count = var.enable_privatelink ? 1 : 0

  name        = "${var.name}-vpce-k8s-ingress"
  description = "HTTP hacia el VPCE del Ingress de Kubernetes"
  vpc_id      = var.vpc_id
}

data "aws_vpc" "this" {
  id = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "consumer_http" {
  count = var.enable_privatelink ? 1 : 0

  security_group_id = aws_security_group.ingress_consumer[0].id
  cidr_ipv4         = data.aws_vpc.this.cidr_block
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_endpoint" "ingress" {
  count = var.enable_privatelink ? 1 : 0

  vpc_id             = var.vpc_id
  service_name       = aws_vpc_endpoint_service.ingress[0].service_name
  vpc_endpoint_type  = "Interface"
  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.ingress_consumer[0].id]

  tags = { Name = "${var.name}-vpce-k8s-ingress" }
}
