// Spike: sudden burst, then observe whether the system recovers.
import { sleepMs, submitRun, readRun } from '../lib/common.js';

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-vus',
      startVUs: 2,
      stages: [
        { duration: '20s', target: 2 },   // calm
        { duration: '5s', target: 60 },   // spike
        { duration: '30s', target: 60 },  // hold
        { duration: '5s', target: 2 },    // drop
        { duration: '40s', target: 2 },   // recovery window
      ],
      gracefulRampDown: '20s',
    },
  },
};

export default function () {
  const runId = submitRun('spike');
  if (runId) {
    readRun(runId);
  }
  sleepMs(300);
}
