variable "project" {
  type    = string
  default = "inventario"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "business_timezone" {
  description = "Zona horaria del horario hábil (L-V 8-18)"
  type        = string
  default     = "America/Bogota"
}

variable "alert_email" {
  description = "Correo del comprador para órdenes de compra y alarmas (vacío = sin suscripción)"
  type        = string
  default     = ""
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "multi_az" {
  description = "true en producción (RNF-06)"
  type        = bool
  default     = false
}

variable "allow_fault_injection" {
  description = "Permite simulate_failure en mensajes SQS (demo). false en producción"
  type        = bool
  default     = true
}

variable "enable_direct_benchmark" {
  description = "Abre RDS a la Lambda bench SOLO para comparar con/sin Proxy. Mantener false"
  type        = bool
  default     = false
}

variable "certificate_arn" {
  description = "ARN de ACM para HTTPS en el ALB (vacío = solo HTTP)"
  type        = string
  default     = ""
}

variable "bedrock_model" {
  description = "Modelo Claude en Bedrock (habilitar acceso al modelo en la consola de Bedrock)"
  type        = string
  default     = "anthropic.claude-haiku-5-5"
}

variable "bedrock_fallback_model" {
  description = "Modelo de respaldo si el principal responde refusal"
  type        = string
  default     = "anthropic.claude-sonnet-5"
}
