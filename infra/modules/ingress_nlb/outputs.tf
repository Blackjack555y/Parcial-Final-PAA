output "nlb_arn" {
  value = aws_lb.ingress.arn
}

output "nlb_dns_name" {
  value = aws_lb.ingress.dns_name
}

output "target_group_arn" {
  value = aws_lb_target_group.ingress.arn
}

output "endpoint_service_name" {
  value = one(aws_vpc_endpoint_service.ingress[*].service_name)
}

output "k8s_ingress_vpce_id" {
  value = one(aws_vpc_endpoint.ingress[*].id)
}
