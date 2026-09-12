// Average load: the journey a real SDK caller performs, with think time.
//
// ARR_VUS controls concurrency so the same script drives the concurrency sweep
// documented in docs/performance-audit.md.
import { sleepMs, submitRun, waitForTerminal, readHistory } from '../lib/common.js';

const VUS = parseInt(__ENV.ARR_VUS || '10', 10);
const DURATION = __ENV.ARR_DURATION || '60s';

export const options = {
  scenarios: {
    journey: {
      executor: 'constant-vus',
      vus: VUS,
      duration: DURATION,
      gracefulStop: '20s',
    },
  },
  thresholds: {
    // Recorded, not enforced: this suite establishes the baseline that future
    // gates compare against. Inventing an SLO here would be guesswork.
    'arr_submit_latency': ['p(95)>=0'],
    'arr_read_latency': ['p(95)>=0'],
  },
};

export default function () {
  const runId = submitRun('load');
  if (!runId) {
    sleepMs(1000);
    return;
  }
  waitForTerminal(runId, 20000, 250);
  readHistory(runId);
  // Think time: a caller does not immediately submit the next run.
  sleepMs(500 + Math.random() * 1000);
}
