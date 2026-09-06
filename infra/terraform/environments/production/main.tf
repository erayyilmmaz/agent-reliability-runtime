terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  backend "http" {}

  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
  }
}

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.kubeconfig_context
}

module "runtime_namespace" {
  source = "../../modules/runtime_namespace"

  namespace           = var.namespace
  labels              = local.labels
  resource_quota_hard = var.resource_quota_hard
}

locals {
  labels = {
    environment                             = var.environment
    "app.kubernetes.io/part-of"             = "agent-reliability-runtime"
    "agent-reliability-runtime/secret-name" = var.existing_runtime_secret_name
  }
}
