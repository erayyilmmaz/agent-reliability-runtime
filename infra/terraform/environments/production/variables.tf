variable "environment" {
  type    = string
  default = "production"

  validation {
    condition     = var.environment == "production"
    error_message = "This root module is only for the production environment."
  }
}

variable "namespace" {
  type    = string
  default = "arr-production"
}

variable "kubeconfig_path" {
  type = string
}

variable "kubeconfig_context" {
  type    = string
  default = null
}

variable "existing_runtime_secret_name" {
  type    = string
  default = "agent-reliability-runtime-secrets"
}

variable "resource_quota_hard" {
  type = map(string)
  default = {
    "limits.cpu"      = "16"
    "limits.memory"   = "32Gi"
    "pods"            = "48"
    "requests.cpu"    = "8"
    "requests.memory" = "16Gi"
  }
}
