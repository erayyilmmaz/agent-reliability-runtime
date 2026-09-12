// Stress: ramp until the system degrades, to locate the capacity knee.
import { sleepMs, submitRun, readRun } from '../lib/common.js';

export const options = {
  scenarios: {
    ramp: {
      executor: 'ramping-vus',
      startVUs: 1,
      stages: [
        { duration: '30s', target: 5 },
        { duration: '30s', target: 10 },
        { duration: '30s', target: 25 },
        { duration: '30s', target: 50 },
        { duration: '30s', target: 100 },
        { duration: '20s', target: 0 },
      ],
      gracefulRampDown: '20s',
    },
  },
};

export default function () {
  const runId = submitRun('stress');
  if (runId) {
    readRun(runId);
  }
  sleepMs(200 + Math.random() * 300);
}
