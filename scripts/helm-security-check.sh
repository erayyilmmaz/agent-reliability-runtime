#!/usr/bin/env bash
# SEC-011 / SEC-012 regression gate: assert the rendered chart keeps its
# security posture. Runs in CI next to `helm lint`.
set -euo pipefail

chart="charts/agent-reliability-runtime"
manifest="$(helm template arr "$chart" --namespace arr)"
failures=0

fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

pass() {
  echo "ok: $1"
}

# --- Every pod spec must drop privileges -----------------------------------
pod_specs=$(printf '%s' "$manifest" | grep -c 'automountServiceAccountToken: false' || true)
workload_count=$(printf '%s' "$manifest" | grep -cE '^kind: (Deployment|Job)$' || true)
if [ "$pod_specs" -ne "$workload_count" ] || [ "$workload_count" -eq 0 ]; then
  fail "expected $workload_count pod specs with automountServiceAccountToken:false, found $pod_specs"
else
  pass "all $workload_count workloads disable service-account token automounting"
fi

for required in \
  'runAsNonRoot: true' \
  'allowPrivilegeEscalation: false' \
  'readOnlyRootFilesystem: true' \
  'type: RuntimeDefault'
do
  found=$(printf '%s' "$manifest" | grep -c "$required" || true)
  if [ "$found" -lt "$workload_count" ]; then
    fail "'$required' appears $found times, expected at least $workload_count"
  else
    pass "'$required' set on all workloads"
  fi
done

if ! printf '%s' "$manifest" | grep -q -- '- ALL'; then
  fail "containers do not drop ALL capabilities"
else
  pass "containers drop ALL capabilities"
fi

# --- SEC-012: the provider key reaches the worker and nothing else ----------
provider_secret=$(printf '%s' "$manifest" | grep -c 'agent-reliability-runtime-provider' || true)
if [ "$provider_secret" -ne 1 ]; then
  fail "provider secret is referenced $provider_secret times, expected exactly 1 (worker only)"
else
  pass "provider secret is mounted into exactly one workload"
fi

worker_block=$(printf '%s' "$manifest" | awk '/^kind: Deployment$/{d=1} d&&/component: worker/{w=1} w&&/agent-reliability-runtime-provider/{print "found"; exit}')
if [ "$worker_block" != "found" ]; then
  fail "the worker does not receive the provider secret"
else
  pass "the worker receives the provider secret"
fi

# --- SEC-OPS-01: background probes must measure progress, not PID 1 ---------
# Match the probe command itself, not the comment explaining why it was dropped.
if printf '%s' "$manifest" | grep -vE '^\s*#' | grep -q 'kill -0 1'; then
  fail "a probe still uses 'kill -0 1'"
else
  pass "no probe relies on 'kill -0 1'"
fi

healthcheck_probes=$(printf '%s' "$manifest" | grep -c 'agent-runtime-healthcheck' || true)
if [ "$healthcheck_probes" -lt 6 ]; then
  fail "expected readiness+liveness heartbeat probes on 3 background workloads, found $healthcheck_probes"
else
  pass "background workloads use heartbeat probes"
fi

if [ "$failures" -gt 0 ]; then
  echo "chart security check failed with $failures problem(s)" >&2
  exit 1
fi
echo "chart security check passed"
