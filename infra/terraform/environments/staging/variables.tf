variable "environment" {
  type    = string
  default = "staging"

  validation {
    condition     = var.environment == "staging"
    error_message = "This root module is only for the staging environment."
  }
}

variable "namespace" {
  type    = string
  default = "arr-staging"
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
    "limits.cpu"      = "8"
    "limits.memory"   = "16Gi"
    "pods"            = "24"
    "requests.cpu"    = "4"
    "requests.memory" = "8Gi"
  }
}
