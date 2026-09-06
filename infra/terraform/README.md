# Terraform baseline

This baseline manages only the Kubernetes namespace and a resource quota for
each environment. A provider-specific module may later provision the cluster
and managed PostgreSQL, Redis, RabbitMQ, registry, or network resources, but
that decision is intentionally not assumed here because it affects accounts,
costs, regions, and operational ownership.

The roots are `environments/sandbox`, `environments/staging`, and
`environments/production`. They all consume the small reusable
`modules/runtime_namespace` module. The `existing_runtime_secret_name` input is
metadata only; Terraform does not create, read, or output secret values.

Run validation without configuring a state backend:

```bash
terraform fmt -check -recursive infra/terraform
terraform -chdir=infra/terraform/environments/sandbox init -backend=false
terraform -chdir=infra/terraform/environments/sandbox validate
```

For an apply, copy the environment `terraform.tfvars.example` and
`backend.hcl.example` to protected locations outside this repository. Then use
the remote HTTP backend configuration explicitly:

```bash
terraform -chdir=infra/terraform/environments/sandbox init \
  -backend-config=/secure/path/arr-sandbox.backend.hcl
terraform -chdir=infra/terraform/environments/sandbox plan \
  -var-file=/secure/path/arr-sandbox.tfvars
```

Do not run Terraform for Compose or kind/k3d development. Those local paths
remain independent and use the Helm guide in
[`docs/deployment/kubernetes.md`](../../docs/deployment/kubernetes.md).
