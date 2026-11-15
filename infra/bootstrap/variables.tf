variable "target" {
  description = "Nube destino: floci (emulador local) o aws (cuenta real)."
  type        = string
  default     = "floci"

  validation {
    condition     = contains(["floci", "aws"], var.target)
    error_message = "target debe ser \"floci\" o \"aws\"."
  }
}

variable "aws_region" {
  description = "Región de AWS."
  type        = string
  default     = "us-east-1"
}

variable "floci_endpoint" {
  description = "URL de Floci (solo con target = floci)."
  type        = string
  default     = "http://localhost:4566"
}

variable "state_bucket_name" {
  description = "Nombre del bucket del estado. En AWS real debe ser único globalmente."
  type        = string
  default     = "aegisvolt-tfstate"
}

variable "noncurrent_version_retention_days" {
  description = "Días que se conservan las versiones anteriores del tfstate (respaldo histórico)."
  type        = number
  default     = 90
}
