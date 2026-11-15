variable "env" {
  type = string
}

variable "deletion_protection" {
  description = "Impide borrar las tablas (true en prod)."
  type        = bool
  default     = false
}

variable "point_in_time_recovery" {
  description = "Respaldo continuo (PITR) de las tablas."
  type        = bool
  default     = true
}

variable "feature_flags" {
  description = "Valores INICIALES de las flags de este ambiente. Después se cambian en caliente."
  type = map(object({
    enabled     = bool
    description = string
  }))
}
