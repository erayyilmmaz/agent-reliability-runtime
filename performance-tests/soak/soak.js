// Soak: steady modest load for a long window, to expose leaks and drift.
//
// ARR_DURATION defaults to 10m for a quick check; use 1h+ in a dedicated
// environment when hunting slow growth.
import { sleepMs, submitRun, waitForTerminal, readHistory } from '../lib/common.js';

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.ARR_VUS || '5', 10),
      duration: __ENV.ARR_DURATION || '10m',
      gracefulStop: '30s',
    },
  },
};

export default function () {
  const runId = submitRun('soak');
  if (!runId) {
    sleepMs(2000);
    return;
  }
  waitForTerminal(runId, 30000, 500);
  readHistory(runId);
  sleepMs(1000);
}
