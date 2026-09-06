output "namespace" {
  description = "The managed runtime namespace."
  value       = kubernetes_namespace_v1.runtime.metadata[0].name
}

output "resource_quota_name" {
  description = "The resource quota that constrains this environment."
  value       = kubernetes_resource_quota_v1.runtime.metadata[0].name
}
