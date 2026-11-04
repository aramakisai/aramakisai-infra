import dataclasses
import unittest
from datetime import datetime, timezone

from helpers import FakeHttp, make_ctx
from model import Status
from sources import festival_cloudflare as fc

NOW = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
ENV = {"OPS_CLOUDFLARE_READ_TOKEN": "t", "TF_VAR_cloudflare_account_id": "ACC"}


def gql(hub_minutes=("2026-06-01T11:57:00Z",)):
    return {"data": {"viewer": {"accounts": [{
        "workers": [{"sum": {"requests": 900, "errors": 5}, "dimensions": {"status": "success"}},
                    {"sum": {"requests": 100, "errors": 8}, "dimensions": {"status": "exceededResources"}}],
        "workersCpu": [{"quantiles": {"cpuTimeP99": 12500}}],
        "cms": [{"count": 700, "dimensions": {"edgeResponseStatus": 200}},
                {"count": 20, "dimensions": {"edgeResponseStatus": 404}},
                {"count": 3, "dimensions": {"edgeResponseStatus": 500}},
                {"count": 2, "dimensions": {"edgeResponseStatus": 503}}],
        "cmsLatency": [{"quantiles": {"originResponseDurationMsP95": 420}}],
        "media": [{"count": 300, "dimensions": {"edgeResponseStatus": 200}},
                  {"count": 7, "dimensions": {"edgeResponseStatus": 404}},
                  {"count": 1, "dimensions": {"edgeResponseStatus": 502}}],
        "cmsCache": [{"count": 80, "dimensions": {"cacheStatus": "hit"}},
                     {"count": 20, "dimensions": {"cacheStatus": "miss"}}],
        "hub": [{"max": {"activeWebsocketConnections": n}, "dimensions": {"datetimeMinute": m}}
                for n, m in zip((5, 9), hub_minutes)],
    }]}}}


def run(resp, cfg=None):
    http = FakeHttp({fc.GRAPHQL: resp})
    r = fc.SOURCES[0].fetch(make_ctx(cfg=cfg, http=http, env=ENV, now=NOW))
    return r, http, {i.key: i for i in r.items}


class CloudflareTest(unittest.TestCase):
    def test_values(self):
        r, http, it = run(gql())
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(it["worker"].values, {
            "requests": 1000, "errors": 13, "exceeded": 100, "cpu_p99_ms": 12.5, "window_end": "2026-06-01T11:57:00Z"})
        self.assertEqual(it["cms"].values, {
            "requests": 725, "status_5xx": 5, "origin_p95_ms": 420, "window_end": "2026-06-01T11:57:00Z"})
        self.assertEqual(it["media"].values, {
            "requests": 308, "status_4xx": 7, "status_5xx": 1, "window_end": "2026-06-01T11:57:00Z"})
        self.assertEqual(it["cms_cache"].values, {"lookups": 100, "hits": 80, "window_end": "2026-06-01T11:57:00Z"})
        self.assertTrue(all(i.status == Status.OK for i in r.items))

    def test_window_and_filters(self):
        _, http, _ = run(gql())
        v = http.calls[0]["body"]["variables"]
        self.assertEqual((v["start"], v["end"]), ("2026-06-01T11:52:00Z", "2026-06-01T11:57:00Z"))
        self.assertEqual(v["tag"], "ACC")
        self.assertEqual((v["script"], v["cmsHost"], v["mediaLike"], v["hub"]),
                         ("aramakisai-web", "cms.aramakisai.com", "/api/media/%", "signage-hub"))
        self.assertEqual(http.calls[0]["bearer"], "t")
        q = http.calls[0]["body"]["query"]
        for s in ("eyeball", "edgeWorkerFetch", "edgeWorkerCacheAPI", "clientRequestHTTPMethodName",
                  "cacheStatus", "cpuTimeP99", "originResponseDurationMsP95"):
            self.assertIn(s, q)
        self.assertNotIn("clientIP", q)
        self.assertNotIn("userAgent", q.lower())

    def test_empty_rows_are_zero(self):
        resp = gql()
        a = resp["data"]["viewer"]["accounts"][0]
        for k in ("workers", "workersCpu", "cms", "cmsLatency", "media", "cmsCache", "hub"):
            a[k] = []
        r, _, it = run(resp)
        self.assertEqual(it["worker"].values["requests"], 0)
        self.assertEqual(it["worker"].values["cpu_p99_ms"], 0)
        self.assertEqual(it["cms"].values["origin_p95_ms"], 0)
        self.assertEqual(it["cms_cache"].values["lookups"], 0)
        self.assertEqual(it["hub_total"].values, {"connections": 0, "minute": None})
        self.assertIsNotNone(it["hub_total"].note)

    def test_hub_latest_minute(self):
        _, _, it = run(gql(("2026-06-01T11:55:00Z", "2026-06-01T11:56:00Z")))
        self.assertEqual(it["hub_total"].values, {"connections": 9, "minute": "2026-06-01T11:56:00Z"})
        self.assertIsNone(it["hub_total"].note)

    def test_hub_sums_objects_in_same_minute(self):
        resp = gql(("2026-06-01T11:56:00Z", "2026-06-01T11:56:00Z"))
        _, _, it = run(resp)
        self.assertEqual(it["hub_total"].values["connections"], 14)

    def test_hub_stale_note(self):
        # floor(now)-4m = 11:56。それより古い最新の分は取得遅延
        _, _, it = run(gql(("2026-06-01T11:54:00Z", "2026-06-01T11:55:00Z")))
        self.assertIsNotNone(it["hub_total"].note)
        self.assertEqual(it["hub_total"].values["minute"], "2026-06-01T11:55:00Z")

    def test_graphql_errors_raise(self):
        with self.assertRaises(RuntimeError):
            run({"errors": [{"message": "boom"}], "data": None})

    def test_no_festival_is_empty_without_call(self):
        cfg = dataclasses.replace(make_ctx().config, festival=None)
        r, http, _ = run(gql(), cfg=cfg)
        self.assertEqual(r.status, Status.EMPTY)
        self.assertEqual(http.calls, [])

    def test_missing_account_raises(self):
        with self.assertRaises(Exception):
            fc.SOURCES[0].fetch(make_ctx(http=FakeHttp({fc.GRAPHQL: gql()}), env={"OPS_CLOUDFLARE_READ_TOKEN": "t"}))


if __name__ == "__main__":
    unittest.main()
