import dataclasses
import unittest
from datetime import datetime, timezone

from helpers import FakeHttp, make_ctx
from model import Status
from sources import festival_cloudflare as fc

NOW = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
CFG_HOSTS = make_ctx().config.festival.cloudflare.traffic_hosts
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
        "traffic": [
            {"count": 600, "dimensions": {"clientRequestHTTPHost": "aramakisai.com", "cacheStatus": "hit"}},
            {"count": 100, "dimensions": {"clientRequestHTTPHost": "aramakisai.com", "cacheStatus": "miss"}},
            {"count": 50, "dimensions": {"clientRequestHTTPHost": "cms.aramakisai.com", "cacheStatus": "dynamic"}},
            {"count": 30, "dimensions": {"clientRequestHTTPHost": "unknown.example.com", "cacheStatus": "hit"}},
            {"count": 20, "dimensions": {"clientRequestHTTPHost": "other.example.com", "cacheStatus": "hit"}}],
        "hub": [{"max": {"activeWebsocketConnections": n}, "dimensions": {"datetimeMinute": m}}
                for n, m in zip((5, 9), hub_minutes)],
    }]}}}


def run(resp, cfg=None):
    http = FakeHttp({fc.GRAPHQL: resp})
    r = fc.SOURCES[0].fetch(make_ctx(cfg=cfg, http=http, env=ENV, now=NOW))
    return r, http, {i.key: i for i in r.items}


def _scalars(item):
    return {k: v for k, v in item.values.items() if not k.startswith("by_")}


class CloudflareTest(unittest.TestCase):
    def test_values(self):
        r, http, it = run(gql())
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(_scalars(it["worker"]), {
            "requests": 1000, "errors": 13, "exceeded": 100, "cpu_p99_ms": 12.5, "window_end": "2026-06-01T11:57:00Z"})
        self.assertEqual(_scalars(it["cms"]), {
            "requests": 725, "status_5xx": 5, "origin_p95_ms": 420, "window_end": "2026-06-01T11:57:00Z"})
        self.assertEqual(_scalars(it["media"]), {
            "requests": 308, "status_4xx": 7, "status_5xx": 1, "window_end": "2026-06-01T11:57:00Z"})
        self.assertEqual(_scalars(it["cms_cache"]), {"lookups": 100, "hits": 80, "window_end": "2026-06-01T11:57:00Z"})
        self.assertTrue(all(i.status == Status.OK for i in r.items))

    def test_breakdowns_match_rows(self):
        resp = gql()
        a = resp["data"]["viewer"]["accounts"][0]
        a["cms"].append({"count": 5, "dimensions": {"edgeResponseStatus": 200}})
        _, _, it = run(resp)
        self.assertEqual(it["worker"].values["by_status"], [
            {"status": "success", "requests": 900, "errors": 5},
            {"status": "exceededResources", "requests": 100, "errors": 8}])
        self.assertEqual(it["cms"].values["by_status"], [
            {"status": 200, "count": 705}, {"status": 404, "count": 20},
            {"status": 500, "count": 3}, {"status": 503, "count": 2}])
        self.assertEqual(it["media"].values["by_status"], [
            {"status": 200, "count": 300}, {"status": 404, "count": 7}, {"status": 502, "count": 1}])
        self.assertEqual(it["cms_cache"].values["by_cache_status"], [
            {"cache_status": "hit", "count": 80}, {"cache_status": "miss", "count": 20}])

    def test_breakdown_rows_are_flat_scalars(self):
        _, _, it = run(gql())
        for i in it.values():
            for k, rows in i.values.items():
                if k.startswith("by_"):
                    for row in rows:
                        self.assertTrue(all(isinstance(v, (int, str)) for v in row.values()))

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
        for k in ("workers", "workersCpu", "cms", "cmsLatency", "media", "cmsCache", "traffic", "hub"):
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

    def test_traffic_items(self):
        r, http, it = run(gql())
        hosts = CFG_HOSTS
        self.assertEqual([i.key for i in r.items if i.key.startswith("traffic.")],
                         ["traffic.total", *[f"traffic.host.{h}" for h in hosts], "traffic.host.other"])
        self.assertEqual(it["traffic.total"].values["requests"], 800)
        self.assertEqual(it["traffic.total"].values["window_end"], "2026-06-01T11:57:00Z")
        by_host = {r["host"]: r["count"] for r in it["traffic.total"].values["by_host"]}
        self.assertEqual(by_host["aramakisai.com"], 700)
        self.assertEqual(by_host["cms.aramakisai.com"], 50)
        self.assertEqual(by_host["other"], 50)  # 一覧外の 2 ホストを合算
        self.assertEqual(by_host["dash.aramakisai.com"], 0)  # 窓内 0 件でも項目を作る
        self.assertEqual(len(by_host), len(hosts) + 1)
        self.assertEqual(it["traffic.host.aramakisai.com"].values["by_cache_status"],
                         [{"cache_status": "hit", "count": 600}, {"cache_status": "miss", "count": 100}])
        self.assertEqual(it["traffic.host.dash.aramakisai.com"].values["requests"], 0)
        q = http.calls[0]["body"]["query"]
        self.assertIn('requestSource: "eyeball"', q)
        self.assertIn("clientRequestHTTPHost cacheStatus", q)

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
