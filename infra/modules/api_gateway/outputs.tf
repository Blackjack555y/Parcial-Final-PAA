output "rest_api_ids" {
  value = { for k, a in aws_api_gateway_rest_api.this : k => a.id }
}

output "invoke_urls" {
  value = { for k, s in aws_api_gateway_stage.this : k => s.invoke_url }
}

output "vpc_link_id" {
  value = aws_api_gateway_vpc_link.this.id
}

output "rest_api_id_public" {
  value = aws_api_gateway_rest_api.this["public"].id
}
