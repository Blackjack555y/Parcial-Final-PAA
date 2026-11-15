variable "cluster_name" {
  type = string
}

variable "kubernetes_version" {
  type    = string
  default = "1.34"
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

variable "endpoint_public_access" {
  description = "API de Kubernetes accesible desde internet. false = solo desde la VPC."
  type        = bool
  default     = false
}

variable "enable_node_group" {
  description = "Crear el grupo de nodos administrado (en Floci los pods corren en minikube)."
  type        = bool
  default     = true
}

variable "node_instance_types" {
  type    = list(string)
  default = ["t3.medium"]
}

variable "node_desired_size" {
  type    = number
  default = 2
}
