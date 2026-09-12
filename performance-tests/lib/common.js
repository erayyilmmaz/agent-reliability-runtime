// Shared helpers for the Agent Reliability Runtime k6 suite.
//
// Every scenario models a real caller of this runtime rather than hammering a
// single URL: the SDK submits a run, polls it to a terminal state, then reads
// its durable history. Looping one endpoint at maximum rate would measure the
// endpoint, not the system.
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter } from 'k6/metrics';

export const BASE_URL = __ENV.ARR_BASE_URL || 'http://localhost:8000';
export const API_KEY = __ENV.ARR_API_KEY || '';
export const CLIENT_ID = __ENV.ARR_CLIENT_ID || 'perf-tenant';

// Per-stage timings, so a slow journey can be attributed to a specific call.
export const submitLatency = new Trend('arr_submit_latency', true);
export const readLatency = new Trend('arr_read_latency', true);
export const attemptsLatency = new Trend('arr_attempts_latency', true);
export const eventsLatency = new Trend('arr_events_latency', true);
export const evaluateLatency = new Trend('arr_evaluate_latency', true);
export const terminalWait = new Trend('arr_time_to_terminal', true);

export const submitRejected = new Counter('arr_submit_rejected');
export const quotaRejected = new Counter('arr_quota_rejected');
export const rateLimited = new Counter('arr_rate_limited');

export function headers(extra) {
  const base = { 'Content-Type': 'application/json', 'X-Client-Id': CLIENT_ID };
  if (API_KEY) {
    base['X-API-Key'] = API_KEY;
  }
  return Object.assign(base, extra || {});
}

// Unique per VU + iteration so idempotency never collapses two journeys into one.
export function idempotencyKey(prefix) {
  return `${prefix}-${__VU}-${__ITER}-${Date.now()}`;
}

export function submitRun(prefix, promptSize) {
  const prompt = promptSize ? 'x'.repeat(promptSize) : 'performance probe';
  const response = http.post(
    `${BASE_URL}/v1/runs`,
    JSON.stringify({ input: { prompt } }),
    { headers: headers({ 'Idempotency-Key': idempotencyKey(prefix) }), tags: { arr_op: 'submit' } },
  );
  submitLatency.add(response.timings.duration);

  if (response.status === 429) {
    rateLimited.add(1);
  } else if (response.status === 409 || response.status === 422) {
    quotaRejected.add(1);
  }
  const ok = check(response, { 'submit accepted (202)': (r) => r.status === 202 });
  if (!ok) {
    submitRejected.add(1);
    return null;
  }
  return response.json('run_id');
}

export function readRun(runId) {
  const response = http.get(`${BASE_URL}/v1/runs/${runId}`, {
    headers: headers(),
    tags: { arr_op: 'read' },
  });
  readLatency.add(response.timings.duration);
  check(response, { 'read run (200)': (r) => r.status === 200 });
  return response.status === 200 ? response.json('execution_status') : null;
}

export function readHistory(runId) {
  const attempts = http.get(`${BASE_URL}/v1/runs/${runId}/attempts`, {
    headers: headers(),
    tags: { arr_op: 'attempts' },
  });
  attemptsLatency.add(attempts.timings.duration);
  check(attempts, { 'read attempts (200)': (r) => r.status === 200 });

  const events = http.get(`${BASE_URL}/v1/runs/${runId}/events`, {
    headers: headers(),
    tags: { arr_op: 'events' },
  });
  eventsLatency.add(events.timings.duration);
  check(events, { 'read events (200)': (r) => r.status === 200 });

  return { attemptBytes: attempts.body.length, eventBytes: events.body.length };
}

// Mirrors AgentRuntimeClient.wait_for_terminal.
export function waitForTerminal(runId, timeoutMs, pollMs) {
  const deadline = Date.now() + (timeoutMs || 15000);
  const started = Date.now();
  const terminal = ['SUCCEEDED', 'FAILED', 'DEAD_LETTERED'];
  while (Date.now() < deadline) {
    const status = readRun(runId);
    if (status === null) return null;
    if (terminal.indexOf(status) !== -1) {
      terminalWait.add(Date.now() - started);
      return status;
    }
    sleepMs(pollMs || 250);
  }
  return 'TIMEOUT';
}

export function sleepMs(ms) {
  // k6's sleep() takes seconds; keeping the conversion in one place avoids the
  // classic 1000x think-time mistake.
  sleep(ms / 1000);
}
