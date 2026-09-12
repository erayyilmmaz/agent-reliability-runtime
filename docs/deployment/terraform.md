# Terraform deployment baseline

## Scope and boundaries

ARR-16 provides a reproducible Kubernetes application-plane baseline, not a
cloud-provider commitment. Terraform creates the environment namespace and a
resource quota; Helm owns the runtime workloads in that namespace. The cluster,
network, registry, managed PostgreSQL, Redis, RabbitMQ, and remote state service
are explicitly external dependencies.

This separation keeps local Compose and kind/k3d workflows Terraform-free and
prevents a portfolio demo from silently creating billable cloud resources.

```text
remote state backend (outside Git)
             |
             v
Terraform environment root -> Kubernetes namespace + resource quota
             |
             v
pre-provisioned Kubernetes Secret (outside Terraform state)
             |
             v
Helm chart -> migration Job, API, worker, dispatcher, scheduler
```

## Environments and cost guardrails

`sandbox`, `staging`, and `production` each have an independent Terraform root
and state address. Their namespace resource quotas grow deliberately from small
sandbox limits to production limits. A quota limits requested/maximum compute;
it is a guardrail, not a cloud billing guarantee. Pick the cloud provider,
region, managed-service tier, and state backend with the account owner before
adding provider-specific modules.

The low-cost development target remains Docker Compose or kind/k3d. Neither
requires Terraform or a cloud account.

## State and secret policy

- The roots declare an HTTP backend, but its endpoint, username, and token are
  provided only through an external `backend.hcl` file.
- `.terraform`, all `*.tfstate`, all real `*.tfvars`, and crash logs are ignored.
- `terraform.tfvars.example` contains cluster paths, context names, and namespace
  names only.
- Terraform receives the name of the pre-created runtime Secret but never its
  data. Use External Secrets, a cloud secret manager, or an operator-approved
  secret process to populate `APP_DATABASE_URL`, `APP_REDIS_URL`,
  `APP_RABBITMQ_URL`, `APP_AUTH_CREDENTIALS`, `APP_AUTH_PEPPER`, and optional provider keys.

## Operational sequence

1. Provision or select a Kubernetes cluster and a remote HTTP Terraform state
   service outside this repository.
2. Create the runtime Secret with the approved secret-management process.
3. Copy the selected environment's `terraform.tfvars.example` and the backend
   example to protected, untracked paths; fill in the cluster context and state
   service values.
4. Run `terraform init`, `plan`, and a reviewed `apply` from the selected root.
5. Install the Helm release into the Terraform-managed namespace, passing the
   same existing Secret name.
6. Confirm the migration Job, API readiness, worker scaling, and observability
   according to the Kubernetes guide.
