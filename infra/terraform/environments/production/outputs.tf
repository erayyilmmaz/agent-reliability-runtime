output "namespace" {
  value = module.runtime_namespace.namespace
}

output "existing_runtime_secret_name" {
  description = "Secret name only; no secret data is exported to Terraform state."
  value       = var.existing_runtime_secret_name
}
