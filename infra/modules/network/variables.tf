variable "name" {
  description = "Prefijo de nombres: aegisvolt-<env>."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR de la VPC. Distinto por ambiente para que nunca se solapen."
  type        = string
}

variable "azs" {
  description = "Zonas de disponibilidad (2 para alta disponibilidad)."
  type        = list(string)
}

variable "enable_nat_gateway" {
  description = "NAT Gateway para salida a internet desde las subredes privadas."
  type        = bool
  default     = true
}

variable "cluster_name" {
  description = "Nombre del clúster EKS (etiquetas de descubrimiento de subredes)."
  type        = string
}
