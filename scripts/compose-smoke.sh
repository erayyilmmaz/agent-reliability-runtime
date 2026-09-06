#!/usr/bin/env bash
set -euo pipefail

arr_health_url="http://localhost:8000/healthz"
arr_api_url="http://localhost:8000/v1/runs"
arr_run_key="compose-smoke-$(date +%s)"

cleanup() {
  docker compose down --remove-orphans
}
trap cleanup EXIT

wait_for_success() {
  arr_target_run_id="$1"
  for _ in $(seq 1 30); do
    arr_status=$(curl --fail --silent --show-error "$arr_api_url/$arr_target_run_id" \
      -H 'X-Client-Id: compose-smoke' | sed -nE 's/.*"execution_status":"([^"]+)".*/\1/p')
    if [ "$arr_status" = "SUCCEEDED" ]; then
      return 0
    fi
    sleep 1
  done
  echo "Compose smoke failed: run $arr_target_run_id did not reach SUCCEEDED." >&2
  return 1
}

docker compose up -d --build

for _ in $(seq 1 30); do
  if curl --fail --silent "$arr_health_url" >/dev/null; then
    break
  fi
  sleep 1
done
curl --fail --silent --show-error "$arr_health_url" >/dev/null

arr_response=$(curl --fail --silent --show-error -X POST "$arr_api_url" \
  -H 'Content-Type: application/json' \
  -H 'X-Client-Id: compose-smoke' \
  -H "Idempotency-Key: $arr_run_key" \
  --data '{"input":{"prompt":"credentials-free deterministic demo"}}')
arr_run_id=$(printf '%s' "$arr_response" | sed -nE 's/.*"run_id":"([^"]+)".*/\1/p')
test -n "$arr_run_id"
wait_for_success "$arr_run_id"

arr_duplicate_response=$(curl --fail --silent --show-error -X POST "$arr_api_url" \
  -H 'Content-Type: application/json' \
  -H 'X-Client-Id: compose-smoke' \
  -H "Idempotency-Key: $arr_run_key" \
  --data '{"input":{"prompt":"credentials-free deterministic demo"}}')
arr_duplicate_id=$(printf '%s' "$arr_duplicate_response" | sed -nE 's/.*"run_id":"([^"]+)".*/\1/p')
test "$arr_duplicate_id" = "$arr_run_id"

arr_evaluation=$(curl --fail --silent --show-error -X POST "$arr_api_url/$arr_run_id/evaluations" \
  -H 'Content-Type: application/json' \
  -H 'X-Client-Id: compose-smoke' \
  --data '{"rules":[{"type":"non_empty","path":"missing"}]}')
printf '%s' "$arr_evaluation" | grep -q '"status":"FAILED"'

arr_replay_response=$(curl --fail --silent --show-error -X POST "$arr_api_url/$arr_run_id/replay" \
  -H 'X-Client-Id: compose-smoke' \
  -H "Idempotency-Key: $arr_run_key-replay")
arr_replay_id=$(printf '%s' "$arr_replay_response" | sed -nE 's/.*"run_id":"([^"]+)".*/\1/p')
test -n "$arr_replay_id"
test "$arr_replay_id" != "$arr_run_id"
wait_for_success "$arr_replay_id"

echo "Compose smoke passed: deterministic run, duplicate, failed evaluation, and replay verified."
