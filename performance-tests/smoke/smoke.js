// Smoke: minimal load, verifies the script and the stack before anything larger.
import { sleepMs, submitRun, waitForTerminal, readHistory } from '../lib/common.js';

export const options = {
  vus: 1,
  iterations: 5,
  thresholds: {
    checks: ['rate==1.00'],
    http_req_failed: ['rate==0.00'],
  },
};

export default function () {
  const runId = submitRun('smoke');
  if (!runId) return;
  waitForTerminal(runId, 20000, 250);
  readHistory(runId);
  sleepMs(500);
}
