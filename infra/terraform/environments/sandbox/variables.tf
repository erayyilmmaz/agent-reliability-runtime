variable "environment" {
  description = "Deployment environment identifier."
  type        = string
  default     = "sandbox"

  validation {
    condition     = var.environment == "sandbox"
    error_message = "This root module is only for the sandbox environment."
  }
}

variable "namespace" {
  description = "Namespace managed by this root module."
  type        = string
  default     = "arr-sandbox"
}

variable "kubeconfig_path" {
  description = "Path to a local kubeconfig. This path is not a secret but is environment-specific."
  type        = string
}

variable "kubeconfig_context" {
  description = "Optional kubeconfig context for the target cluster."
  type        = string
  default     = null
}

variable "existing_runtime_secret_name" {
  description = "Name only of the pre-provisioned Kubernetes Secret consumed by Helm; Terraform never reads its values."
  type        = string
  default     = "agent-reliability-runtime-secrets"
}

variable "resource_quota_hard" {
  description = "Sandbox resource ceiling used as a cost-control guardrail."
  type        = map(string)
  default = {
    "limits.cpu"      = "4"
    "limits.memory"   = "8Gi"
    "pods"            = "12"
    "requests.cpu"    = "2"
    "requests.memory" = "4Gi"
  }
}
