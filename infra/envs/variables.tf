variable "target" {
  description = "Nube destino: floci (emulador local) o aws (cuenta real)."
  type        = string

  validation {
    condition     = contains(["floci", "aws"], var.target)
    error_message = "target debe ser \"floci\" o \"aws\"."
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "floci_endpoint" {
  description = "URL de Floci (solo con target = floci)."
  type        = string
  default     = "http://localhost:4566"
}

variable "azs" {
  description = "Zonas de disponibilidad."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b"]
}

variable "vpc_cidrs" {
  description = "CIDR por ambiente: rangos distintos para poder interconectar VPC sin solapes."
  type        = map(string)
  default = {
    dev  = "10.10.0.0/16"
    test = "10.20.0.0/16"
    prod = "10.30.0.0/16"
  }
}

variable "enable_nat_gateway" {
  type    = bool
  default = true
}

variable "enable_privatelink" {
  description = "VPC Endpoint Service (PrivateLink) hacia el Ingress de Kubernetes."
  type        = bool
  default     = true
}

variable "eks_enable_node_group" {
  type    = bool
  default = true
}

variable "eks_endpoint_public_access" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  description = "Retención de logs por ambiente."
  type        = map(number)
  default = {
    dev  = 7
    test = 14
    prod = 30
  }
}

variable "feature_flags" {
  description = "Valores iniciales de las feature flags por ambiente (después se cambian en caliente)."
  type = map(map(object({
    enabled     = bool
    description = string
  })))
  default = {
    dev = {
      ENABLE_DYNAMIC_SURGE_PRICING    = { enabled = true, description = "Tarifa dinámica en horas pico" }
      ENABLE_FAST_CHARGE_BOOST        = { enabled = true, description = "Reserva prioritaria de ultra-carga" }
      SIMULATE_CHARGER_HARDWARE_FAULT = { enabled = false, description = "Inyecta falla del cargador (rollback SAGA)" }
    }
    test = {
      ENABLE_DYNAMIC_SURGE_PRICING    = { enabled = true, description = "Tarifa dinámica en horas pico" }
      ENABLE_FAST_CHARGE_BOOST        = { enabled = true, description = "Reserva prioritaria de ultra-carga" }
      SIMULATE_CHARGER_HARDWARE_FAULT = { enabled = false, description = "Inyecta falla del cargador (rollback SAGA)" }
    }
    prod = {
      ENABLE_DYNAMIC_SURGE_PRICING    = { enabled = false, description = "Tarifa dinámica en horas pico" }
      ENABLE_FAST_CHARGE_BOOST        = { enabled = false, description = "Reserva prioritaria de ultra-carga" }
      SIMULATE_CHARGER_HARDWARE_FAULT = { enabled = false, description = "Inyecta falla del cargador (rollback SAGA)" }
    }
  }
}
