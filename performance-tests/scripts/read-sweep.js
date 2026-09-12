// Isolates the API tier: one authenticated read per iteration, no worker
// pipeline and no think time, so the result is the API's own service curve.
import http from 'k6/http';
import { check } from 'k6';
import { BASE_URL, headers } from '../lib/common.js';

const RUN_ID = __ENV.ARR_RUN_ID;

export const options = {
  scenarios: {
    sweep: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.ARR_VUS || '1', 10),
      duration: __ENV.ARR_DURATION || '20s',
      gracefulStop: '10s',
    },
  },
};

export default function () {
  const response = http.get(`${BASE_URL}/v1/runs/${RUN_ID}`, { headers: headers() });
  check(response, { 'read ok': (r) => r.status === 200 });
}
