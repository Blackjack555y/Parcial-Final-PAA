variable "name" {
  type = string
}

variable "env" {
  type = string
}

variable "nlb_arn" {
  type = string
}

variable "nlb_dns_name" {
  type = string
}

variable "execute_api_vpce_id" {
  description = "VPCE execute-api: único origen permitido para la API interna."
  type        = string
}

variable "throttle_rate_limit" {
  type    = number
  default = 100
}

variable "throttle_burst_limit" {
  type    = number
  default = 200
}
