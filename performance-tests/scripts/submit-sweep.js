// Write path only: submit a run per iteration. Exercises quota admission,
// the per-identity advisory lock, and the transactional outbox insert.
import { submitRun } from '../lib/common.js';

export const options = {
  scenarios: {
    submits: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.ARR_VUS || '1', 10),
      duration: __ENV.ARR_DURATION || '20s',
      gracefulStop: '15s',
    },
  },
};

export default function () {
  submitRun('sweep');
}
