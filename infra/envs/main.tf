# AegisVolt — infraestructura de UN ambiente (el del workspace activo).

module "network" {
  source = "../modules/network"

  name               = local.name
  vpc_cidr           = var.vpc_cidrs[local.env]
  azs                = var.azs
  enable_nat_gateway = var.enable_nat_gateway
  cluster_name       = "${local.name}-eks"

  depends_on = [terraform_data.workspace_guard]
}

module "vpc_endpoints" {
  source = "../modules/vpc_endpoints"

  name                    = local.name
  env                     = local.env
  aws_region              = var.aws_region
  vpc_id                  = module.network.vpc_id
  vpc_cidr                = module.network.vpc_cidr
  private_subnet_ids      = module.network.private_subnet_ids
  private_route_table_ids = module.network.private_route_table_ids
}

module "dynamodb" {
  source = "../modules/dynamodb"

  env                    = local.env
  deletion_protection    = local.is_prod
  point_in_time_recovery = true
  feature_flags          = var.feature_flags[local.env]

  depends_on = [terraform_data.workspace_guard]
}

module "observability" {
  source = "../modules/observability"

  env            = local.env
  services       = local.services
  retention_days = var.log_retention_days[local.env]

  depends_on = [terraform_data.workspace_guard]
}

module "eks" {
  source = "../modules/eks"

  cluster_name           = "${local.name}-eks"
  vpc_id                 = module.network.vpc_id
  vpc_cidr               = module.network.vpc_cidr
  private_subnet_ids     = module.network.private_subnet_ids
  endpoint_public_access = var.eks_endpoint_public_access
  enable_node_group      = var.eks_enable_node_group
}

module "ingress_nlb" {
  source = "../modules/ingress_nlb"

  name               = local.name
  vpc_id             = module.network.vpc_id
  private_subnet_ids = module.network.private_subnet_ids
  enable_privatelink = var.enable_privatelink
}

module "api_gateway" {
  source = "../modules/api_gateway"

  name                = local.name
  env                 = local.env
  nlb_arn             = module.ingress_nlb.nlb_arn
  nlb_dns_name        = module.ingress_nlb.nlb_dns_name
  execute_api_vpce_id = module.vpc_endpoints.execute_api_endpoint_id
}
