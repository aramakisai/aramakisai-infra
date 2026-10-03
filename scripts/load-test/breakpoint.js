import http from 'k6/http';
import { check } from 'k6';
import { Rate } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const TARGET_PATH = __ENV.TARGET_PATH || '/api/globals/festival_meta?depth=1';
const START_RATE = Number(__ENV.START_RATE || 1);
const MAX_RATE = Number(__ENV.MAX_RATE || 50);
const RAMP_DURATION = __ENV.RAMP_DURATION || '10m';
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 50);
const MAX_VUS = Number(__ENV.MAX_VUS || 500);
// abortOnFail はエラー率がこの値を超えた時点でテストを即時停止する。本番へ印加するため、壊れた後も負荷を掛け続けないための安全弁。
const ERROR_RATE_THRESHOLD = Number(__ENV.ERROR_RATE_THRESHOLD || 0.1);

const errorRate = new Rate('error_rate');

export const options = {
  scenarios: {
    breakpoint: {
      executor: 'ramping-arrival-rate',
      startRate: START_RATE,
      timeUnit: '1s',
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
      // plateau・ramp-down を設けない: 単一ステージで MAX_RATE まで上げ続け、エラー率超過による abortOnFail で止める
      stages: [{ target: MAX_RATE, duration: RAMP_DURATION }],
    },
  },
  thresholds: {
    error_rate: [{ threshold: `rate<${ERROR_RATE_THRESHOLD}`, abortOnFail: true }],
    http_req_failed: [{ threshold: `rate<${ERROR_RATE_THRESHOLD}`, abortOnFail: true }],
  },
};

export default function () {
  const res = http.get(`${BASE_URL}${TARGET_PATH}`, { timeout: '10s' });
  const ok = check(res, { 'status is 2xx/3xx': (r) => r.status >= 200 && r.status < 400 });
  errorRate.add(!ok);
}
