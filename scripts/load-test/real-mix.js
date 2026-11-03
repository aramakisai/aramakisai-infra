import http from 'k6/http';
import { check } from 'k6';
import { Rate } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080';
const START_RATE = Number(__ENV.START_RATE || 1);
const MAX_RATE = Number(__ENV.MAX_RATE || 50);
const RAMP_DURATION = __ENV.RAMP_DURATION || '10m';
const HOLD_DURATION = __ENV.HOLD_DURATION || '0s';
const PRE_ALLOCATED_VUS = Number(__ENV.PRE_ALLOCATED_VUS || 50);
const MAX_VUS = Number(__ENV.MAX_VUS || 500);
// abortOnFail はエラー率がこの値を超えた時点でテストを即時停止する。本番へ印加するため、壊れた後も負荷を掛け続けないための安全弁。
const ERROR_RATE_THRESHOLD = Number(__ENV.ERROR_RATE_THRESHOLD || 0.1);
const SUMMARY_TOP = Number(__ENV.SUMMARY_TOP || 30);

// 比率データは本番の実測から逆算できるためリポジトリに置かず、実行時にファイルで渡す
const MIX = JSON.parse(open(__ENV.MIX_FILE));
const CUMULATIVE = [];
MIX.reduce((sum, m, i) => (CUMULATIVE[i] = sum + m.weight), 0);
const TOTAL_WEIGHT = CUMULATIVE[CUMULATIVE.length - 1];
const pathOf = (url) => url.split('?')[0];
const PATHS = [...new Set(MIX.map((m) => pathOf(m.url)))];

const errorRate = new Rate('error_rate');

export const options = {
  // handleSummary が p(99) を読むため、既定の集計項目に含まれない p(99) を足す
  summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)'],
  scenarios: {
    real_mix: {
      executor: 'ramping-arrival-rate',
      startRate: START_RATE,
      timeUnit: '1s',
      preAllocatedVUs: PRE_ALLOCATED_VUS,
      maxVUs: MAX_VUS,
      // 保持段階は時間差で効く劣化 (待ち行列の蓄積や CPU throttling の継続) を見るため。既定 0 では限界探索として ramp のみ、エラー率超過による abortOnFail で止める。ramp-down は設けない
      stages: [
        { target: MAX_RATE, duration: RAMP_DURATION },
        ...(/^0+(\.0+)?(ms|s|m|h)?$/.test(HOLD_DURATION) ? [] : [{ target: MAX_RATE, duration: HOLD_DURATION }]),
      ],
    },
  },
  thresholds: {
    error_rate: [{ threshold: `rate<${ERROR_RATE_THRESHOLD}`, abortOnFail: true }],
    http_req_failed: [{ threshold: `rate<${ERROR_RATE_THRESHOLD}`, abortOnFail: true }],
    // 空の閾値は、path ごとのサブメトリクスをサマリへ出すためだけに置く
    ...Object.fromEntries(
      PATHS.flatMap((p) => [`http_req_duration{name:${p}}`, `error_rate{name:${p}}`].map((k) => [k, []])),
    ),
  },
};

function pick() {
  const r = Math.random() * TOTAL_WEIGHT;
  let lo = 0;
  let hi = CUMULATIVE.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (CUMULATIVE[mid] > r) hi = mid;
    else lo = mid + 1;
  }
  return MIX[lo].url;
}

export default function () {
  // 公開 API の where は「現在時刻(分単位)」を含み、フロントエンドは分ごとに別 URL を送る
  const now = encodeURIComponent(`${new Date().toISOString().slice(0, 16)}:00.000Z`);
  const url = pick().replace('__NOW__', now);
  const res = http.get(`${BASE_URL}${url}`, { timeout: '10s', tags: { name: pathOf(url) } });
  const ok = check(res, { 'status is 2xx/3xx': (r) => r.status >= 200 && r.status < 400 });
  errorRate.add(!ok, { name: pathOf(url) });
}

export function handleSummary(data) {
  const rows = [];
  for (const p of PATHS) {
    const d = data.metrics[`http_req_duration{name:${p}}`];
    const e = data.metrics[`error_rate{name:${p}}`];
    if (!d || !e || e.values.passes + e.values.fails === 0) continue;
    rows.push({ p, n: e.values.passes + e.values.fails, err: e.values.rate, avg: d.values.avg, p95: d.values['p(95)'] });
  }
  rows.sort((a, b) => b.n - a.n);
  const f = (x) => x.toFixed(1);
  const lines = rows
    .slice(0, SUMMARY_TOP)
    .map((r) => `${String(r.n).padStart(7)} ${f(r.err * 100).padStart(6)}% ${f(r.avg).padStart(9)} ${f(r.p95).padStart(9)}  ${r.p}`);
  const t = data.metrics.http_req_duration.values;
  const out = [
    `paths=${rows.length} (top ${Math.min(SUMMARY_TOP, rows.length)} by count)`,
    '  count    err%  avg(ms)   p95(ms)  path',
    ...lines,
    `total: reqs=${data.metrics.http_reqs.values.count} err=${f(data.metrics.error_rate.values.rate * 100)}% avg=${f(t.avg)}ms p95=${f(t['p(95)'])}ms p99=${f(t['p(99)'])}ms`,
    '',
  ].join('\n');
  return { stdout: out };
}
