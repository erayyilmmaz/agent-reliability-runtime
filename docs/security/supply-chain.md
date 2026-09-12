# Supply chain and delivery integrity

SEC-E5 makes the build inputs immutable and continuously scanned, and gives a
published image verifiable provenance.

Covers SEC-015, SEC-016, SEC-023 and SEC-SC-01. SEC-SC-02 was withdrawn — see
the correction at the end.

---

## 1. Immutable CI inputs (SEC-015)

Every third-party Action is pinned to a commit SHA. A Git tag is mutable: if a
maintainer account is compromised, `@v6` can be repointed and every subsequent
run executes attacker code inside the workspace.

| Action | Pin |
| ------ | --- |
| `actions/checkout` | `d23441a4…` (v6) |
| `actions/setup-python` | `ece7cb06…` (v6) |
| `astral-sh/setup-uv` | `c771a70e…` (v9.0.0) |
| `azure/setup-helm` | `dda3372f…` (v5.0.0) |
| `hashicorp/setup-terraform` | `dfe3c3f8…` (v4) |
| `gitleaks/gitleaks-action` | `e0c47f4f…` (v3.0.0) |
| `aquasecurity/trivy-action` | `ed142fd0…` (v0.36.0) |
| `anchore/sbom-action` | `3ad72834…` (v0.24.2) |
| `actions/upload-artifact` | `043fb46d…` (v7.0.1) |

Every SHA was resolved from the GitHub API rather than written from memory.

Pins only stay safe if something updates them, so `.github/dependabot.yml`
raises weekly PRs for Actions, the Dockerfile and `uv.lock`.

---

## 2. Security gates (SEC-015)

A `security` job runs on every push and pull request, **in parallel with
`quality`** rather than behind it, so a security failure is never masked by an
unrelated lint error.

| Gate | Tool | Fails the build on |
| ---- | ---- | ------------------ |
| Dependency vulnerabilities | `pip-audit` | any known advisory in the locked tree |
| Secrets | `gitleaks` | a credential anywhere in full history |
| Static analysis | `semgrep` (`p/python`, `p/security-audit`, `p/secrets`, `p/dockerfile`, `p/terraform`) | any matched rule |
| Container vulnerabilities | `trivy` | CRITICAL/HIGH with a fix available |
| SBOM | `anchore/sbom-action` | — (publishes a CycloneDX artifact) |

**On the pip-audit invocation.** It audits the resolved virtualenv rather than
a requirements file. `pip-audit --requirement` builds a throwaway venv through
`ensurepip`, which aborts on some runners. `--skip-editable` excludes the local
project itself, which has no PyPI release; `--strict` is deliberately *not*
used, because it turns that expected skip into a failure. Real vulnerabilities
still exit non-zero — verified by downgrading `pytest` to a known-vulnerable
version and confirming exit code 1.

---

## 3. Reproducible image (SEC-016)

```dockerfile
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
COPY --from=ghcr.io/astral-sh/uv:0.6.3@sha256:8257f3d17fd04794feaf89d83b4ccca3b2eaa5501de9399fa53929843c0a5b55 /uv /uvx /bin/
```

Both are **multi-arch index** digests, not per-platform manifest digests, so the
build works on CI's amd64 and on an arm64 workstation. Refresh with:

```bash
docker buildx imagetools inspect python:3.12-slim --format '{{.Manifest.Digest}}'
```

### Deploy by digest

The chart prefers `image.digest` over `image.tag`:

```bash
helm upgrade --install arr charts/agent-reliability-runtime \
  --set image.repository=ghcr.io/erayyilmmaz/agent-reliability-runtime \
  --set image.digest=sha256:...
```

`pullPolicy` now defaults to `Always`, which matters while mutable tags are in
use; with a digest it is redundant but harmless.

### HEALTHCHECK

One image serves four entrypoints, so a single health check is only correct if
all four report health the same way. All four now publish a heartbeat — the API
from a task in its FastAPI lifespan, which also means a blocked event loop
(exactly what a runaway evaluation rule would cause) fails the check.

```dockerfile
HEALTHCHECK CMD ["sh", "-c", "test -z \"$APP_HEARTBEAT_PATH\" || agent-runtime-healthcheck --max-age 60"]
```

It skips when `APP_HEARTBEAT_PATH` is unset rather than passing vacuously, and
Kubernetes overrides it with the chart's own probes.

`agent-runtime-healthcheck` reads `APP_HEARTBEAT_PATH` straight from the
environment and never builds a `Settings` object. A probe that fails because
some unrelated configuration is invalid would report a healthy process as dead
and restart it into the same broken config.

---

## 4. Signed releases with provenance (SEC-SC-01)

`.github/workflows/release.yml` triggers **only on a `v*.*.*` tag** (or a manual
dispatch naming an existing tag). Nothing in it runs on branch pushes or pull
requests, so it never executes untrusted input.

It publishes to GHCR and attaches:

1. a **SLSA build-provenance attestation** signed with the workflow's OIDC
   identity — keyless, no long-lived signing key to steal
2. a **CycloneDX SBOM attestation**
3. buildkit's own `provenance: mode=max` and `sbom: true` metadata

Consumers verify before deploying:

```bash
gh attestation verify \
  oci://ghcr.io/erayyilmmaz/agent-reliability-runtime@sha256:... \
  --repo erayyilmmaz/agent-reliability-runtime
```

Permissions are scoped at the job, not the workflow: `packages: write`,
`id-token: write`, `attestations: write`. The workflow-level default stays
`contents: read`.

> **Not yet exercised.** No tag has been pushed, so this workflow has never
> run. Treat the first release as a dry run and verify the attestation before
> relying on it.

---

## 5. Dev dependency advisory (SEC-023)

`pytest` 8.4.2 carried PYSEC-2026-1845 / CVE-2025-71176 (predictable
`/tmp/pytest-of-{user}` paths). It was never in the production image —
`uv sync --no-dev` excludes the whole dev group — so this was hygiene, not a
production risk.

Upgraded to `pytest` 9.1.1. That required `pytest-asyncio >= 1.4` as well;
`<1` cannot resolve against pytest 9. All 232 tests pass on the new major
version, and `pip-audit` now reports **no known vulnerabilities** across the
locked tree.

---

## 6. Correction: SEC-SC-02 was a false positive

The original audit reported ~100 MB of Terraform provider binaries as
"checked in… they predate the ignore rule or were force-added."

**That was wrong.** Verified:

```bash
git ls-files | grep -c '\.terraform/'        # 0
git log --all --oneline -- '*.terraform/*'   # empty
git check-ignore -v .../terraform-provider-kubernetes_v2.38.0_x5
#   .gitignore:14:**/.terraform/
gh api repos/erayyilmmaz/agent-reliability-runtime --jq '.size'   # 1007 KB
```

The binaries exist only in the local working tree as `terraform init` cache,
are correctly gitignored, were never committed, and were never pushed — the
whole remote repository is about 1 MB.

The audit's file inventory came from `find`, which lists working-tree files;
concluding from it that they were tracked was an error on my part. No action is
needed and the ticket should be closed as invalid.

---

## 7. Residual gaps

| Gap | Status |
| --- | ------ |
| Branch protection requiring the `security` job | Not visible in-repo; configure in repository settings |
| Release workflow executed at least once | Pending the first tag |
| `uv` 0.6.3 in the image vs 0.11.x locally | Intentionally unchanged; 0.6.3 reads the current lockfile (verified by the Compose smoke test). Revisit as a separate change |
| Helm chart signing / provenance | Not implemented; only the container image is signed |
