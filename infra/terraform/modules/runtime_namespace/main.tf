resource "kubernetes_namespace_v1" "runtime" {
  metadata {
    name = var.namespace

    labels = merge(
      {
        "app.kubernetes.io/part-of" = "agent-reliability-runtime"
        "managed-by"                = "terraform"
      },
      var.labels,
    )
  }
}

resource "kubernetes_resource_quota_v1" "runtime" {
  metadata {
    name      = "runtime-budget"
    namespace = kubernetes_namespace_v1.runtime.metadata[0].name

    labels = merge(
      {
        "app.kubernetes.io/part-of" = "agent-reliability-runtime"
        "managed-by"                = "terraform"
      },
      var.labels,
    )
  }

  spec {
    hard = var.resource_quota_hard
  }
}
