variable "env" {
  type = string
}

variable "services" {
  description = "Servicios con log group propio."
  type        = list(string)
}

variable "retention_days" {
  type = number
}
