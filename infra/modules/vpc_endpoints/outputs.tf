output "dynamodb_endpoint_id" {
  value = aws_vpc_endpoint.dynamodb.id
}

output "s3_endpoint_id" {
  value = aws_vpc_endpoint.s3.id
}

output "interface_endpoint_ids" {
  value = { for k, v in aws_vpc_endpoint.interface : k => v.id }
}

output "execute_api_endpoint_id" {
  description = "VPCE de API Gateway: la API interna solo acepta tráfico que entre por aquí."
  value       = aws_vpc_endpoint.interface["execute-api"].id
}

output "interface_security_group_id" {
  value = aws_security_group.interface_endpoints.id
}
