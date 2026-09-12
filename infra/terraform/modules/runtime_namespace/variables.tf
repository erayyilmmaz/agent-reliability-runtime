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

variable "pod_security_standard" {
  description = "Pod Security Admission level enforced on the namespace. Empty string disables PSA labelling."
  type        = string
  default     = "restricted"

  validation {
    condition     = contains(["", "privileged", "baseline", "restricted"], var.pod_security_standard)
    error_message = "pod_security_standard must be empty, privileged, baseline, or restricted."
  }
}

variable "pod_security_version" {
  description = "Pod Security Admission version pin. 'latest' tracks the cluster's current policy."
  type        = string
  default     = "latest"
}

variable "network_policy_enabled" {
  description = "Create default-deny ingress plus the explicit allow rules the runtime needs."
  type        = bool
  default     = true
}

variable "ingress_namespace_label" {
  description = "kubernetes.io/metadata.name of the namespace allowed to reach the API port. Empty string skips the rule."
  type        = string
  default     = ""
}

variable "limit_range_enabled" {
  description = "Constrain per-container resources so one pod cannot claim the whole namespace quota."
  type        = bool
  default     = true
}

variable "limit_range_default" {
  description = "Default container resource limits applied when a workload omits them."
  type        = map(string)
  default = {
    cpu    = "500m"
    memory = "512Mi"
  }
}

variable "limit_range_default_request" {
  description = "Default container resource requests applied when a workload omits them."
  type        = map(string)
  default = {
    cpu    = "100m"
    memory = "128Mi"
  }
}

variable "limit_range_max" {
  description = "Maximum resources a single container may request in this namespace."
  type        = map(string)
  default = {
    cpu    = "2"
    memory = "2Gi"
  }
}
