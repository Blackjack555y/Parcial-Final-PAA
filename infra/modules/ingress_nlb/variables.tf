variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "ingress_port" {
  description = "Puerto del Ingress Controller de Kubernetes al que el NLB envía el tráfico."
  type        = number
  default     = 80
}

variable "enable_privatelink" {
  description = "Publicar el Ingress como VPC Endpoint Service (PrivateLink). Floci no lo soporta."
  type        = bool
  default     = true
}
