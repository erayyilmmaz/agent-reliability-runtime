locals {
  base_labels = {
    "app.kubernetes.io/part-of" = "agent-reliability-runtime"
    "managed-by"                = "terraform"
  }

  # SEC-019: Pod Security Admission enforces the same controls the Helm chart
  # sets on each pod, so a future workload cannot opt out of them by omission.
  pod_security_labels = var.pod_security_standard == "" ? {} : {
    "pod-security.kubernetes.io/enforce"         = var.pod_security_standard
    "pod-security.kubernetes.io/enforce-version" = var.pod_security_version
    "pod-security.kubernetes.io/audit"           = var.pod_security_standard
    "pod-security.kubernetes.io/audit-version"   = var.pod_security_version
    "pod-security.kubernetes.io/warn"            = var.pod_security_standard
    "pod-security.kubernetes.io/warn-version"    = var.pod_security_version
  }
}

resource "kubernetes_namespace_v1" "runtime" {
  metadata {
    name = var.namespace

    labels = merge(
      local.base_labels,
      local.pod_security_labels,
      var.labels,
    )
  }
}

resource "kubernetes_resource_quota_v1" "runtime" {
  metadata {
    name      = "runtime-budget"
    namespace = kubernetes_namespace_v1.runtime.metadata[0].name

    labels = merge(local.base_labels, var.labels)
  }

  spec {
    hard = var.resource_quota_hard
  }
}

# SEC-019: the quota caps the namespace total; without a LimitRange a single
# pod can still claim all of it.
resource "kubernetes_limit_range_v1" "runtime" {
  count = var.limit_range_enabled ? 1 : 0

  metadata {
    name      = "runtime-container-limits"
    namespace = kubernetes_namespace_v1.runtime.metadata[0].name

    labels = merge(local.base_labels, var.labels)
  }

  spec {
    limit {
      type            = "Container"
      default         = var.limit_range_default
      default_request = var.limit_range_default_request
      max             = var.limit_range_max
    }
  }
}

# SEC-019: default-deny ingress. Explicit allow rules below re-open only the
# paths the runtime actually uses.
resource "kubernetes_network_policy_v1" "default_deny_ingress" {
  count = var.network_policy_enabled ? 1 : 0

  metadata {
    name      = "default-deny-ingress"
    namespace = kubernetes_namespace_v1.runtime.metadata[0].name

    labels = merge(local.base_labels, var.labels)
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress"]
  }
}

# The API, worker, dispatcher and scheduler all reach PostgreSQL, Redis and
# RabbitMQ inside this namespace, so same-namespace traffic stays allowed.
resource "kubernetes_network_policy_v1" "allow_same_namespace" {
  count = var.network_policy_enabled ? 1 : 0

  metadata {
    name      = "allow-same-namespace-ingress"
    namespace = kubernetes_namespace_v1.runtime.metadata[0].name

    labels = merge(local.base_labels, var.labels)
  }

  spec {
    pod_selector {}
    policy_types = ["Ingress"]

    ingress {
      from {
        pod_selector {}
      }
    }
  }
}

# HTTP reaches the API only from the namespace that runs the ingress
# controller, and only on the API port.
resource "kubernetes_network_policy_v1" "allow_api_ingress" {
  count = var.network_policy_enabled && var.ingress_namespace_label != "" ? 1 : 0

  metadata {
    name      = "allow-api-ingress"
    namespace = kubernetes_namespace_v1.runtime.metadata[0].name

    labels = merge(local.base_labels, var.labels)
  }

  spec {
    pod_selector {
      match_labels = {
        "app.kubernetes.io/component" = "api"
      }
    }

    policy_types = ["Ingress"]

    ingress {
      from {
        namespace_selector {
          match_labels = {
            "kubernetes.io/metadata.name" = var.ingress_namespace_label
          }
        }
      }

      ports {
        port     = "8000"
        protocol = "TCP"
      }
    }
  }
}
