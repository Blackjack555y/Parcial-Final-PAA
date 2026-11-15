variable "name" {
  type = string
}

variable "env" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "private_route_table_ids" {
  type = list(string)
}

variable "interface_services" {
  description = "Servicios con Interface Endpoint (ENI con IP privada en cada subred privada)."
  type        = list(string)
  default     = ["execute-api", "logs", "monitoring"]
}
