output "environment" {
  value = local.env
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "vpc_endpoints" {
  value = merge(
    {
      dynamodb = module.vpc_endpoints.dynamodb_endpoint_id
      s3       = module.vpc_endpoints.s3_endpoint_id
    },
    module.vpc_endpoints.interface_endpoint_ids,
    var.enable_privatelink ? { k8s-ingress = module.ingress_nlb.k8s_ingress_vpce_id } : {},
  )
}

output "dynamodb_tables" {
  value = module.dynamodb.table_names
}

output "log_groups" {
  value = module.observability.log_group_names
}

output "metrics_namespace" {
  value = module.observability.metrics_namespace
}

output "eks_cluster_name" {
  value = module.eks.cluster_name
}

output "api_invoke_urls" {
  value = module.api_gateway.invoke_urls
}
