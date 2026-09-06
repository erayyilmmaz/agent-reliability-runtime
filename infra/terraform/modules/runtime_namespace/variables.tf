variable "namespace" {
  description = "Namespace reserved for one Agent Reliability Runtime environment."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.namespace)) && length(var.namespace) <= 63
    error_message = "namespace must be a DNS-compatible Kubernetes name of at most 63 characters."
  }
}

variable "labels" {
  description = "Non-sensitive labels applied to the namespace and quota."
  type        = map(string)
  default     = {}
}

variable "resource_quota_hard" {
  description = "Hard namespace resource caps expressed with Kubernetes quantity strings."
  type        = map(string)
}
