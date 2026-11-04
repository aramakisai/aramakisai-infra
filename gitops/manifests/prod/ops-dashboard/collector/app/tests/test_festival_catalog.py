import dataclasses
import unittest
from datetime import datetime, timezone
from pathlib import Path

import config
from festival import catalog
from festival.catalog import CATALOG, EvalInput, Judgement
from model import Item, SourceResult, Status

PROD_TOML = Path(__file__).resolve().parents[2] / "dashboard.toml"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CFG = config.load(PROD_TOML).festival


def result(sid, items):
    return SourceResult(sid, Status.OK, NOW, NOW, tuple(items))


def cf_result(**over):
    vals = {
        "worker": {"requests": 1000, "errors": 0, "exceeded": 0, "cpu_p99_ms": 10, "window_end": "2026-06-01T11:57:00Z"},
        "cms": {"requests": 1000, "status_5xx": 0, "origin_p95_ms": 100, "window_end": "2026-06-01T11:57:00Z"},
        "media": {"requests": 10, "status_4xx": 0, "status_5xx": 0, "window_end": "2026-06-01T11:57:00Z"},
    }
    for k, patch in over.items():
        vals[k] = {**vals[k], **patch}
    return result("festival.cloudflare", [Item(k, k, Status.OK, v) for k, v in vals.items()])


def judge(results, cfg=CFG):
    inp = EvalInput(NOW, cfg, results, {}, ())
    return {j.key: j for g in CATALOG for j in g.judge(inp)}


def with_thresholds(**kw):
    return dataclasses.replace(CFG, thresholds={**CFG.thresholds, **kw})


class GradeTest(unittest.TestCase):
    def test_above_boundaries_are_inclusive(self):
        th = config.Threshold(1, 10)
        g = lambda v: catalog.grade("worker_exceeded", th, v)
        self.assertEqual([g(0.9), g(1), g(9.9), g(10)], [Status.OK, Status.WARN, Status.WARN, Status.CRIT])

    def test_below_boundaries_are_exclusive(self):
        th = config.Threshold(4, 1)
        g = lambda v: catalog.grade("tunnel_connections", th, v)
        self.assertEqual([g(4), g(3), g(1), g(0)], [Status.OK, Status.WARN, Status.WARN, Status.CRIT])

    def test_threshold_text(self):
        self.assertEqual(catalog.threshold_text("worker_error_rate", config.Threshold(0.01, 0.05), catalog._pct),
                         "注意 ≥ 1.0% / 異常 ≥ 5.0%")
        self.assertEqual(catalog.threshold_text("tunnel_connections", config.Threshold(4, 1), catalog._num),
                         "注意 < 4 / 異常 < 1")

    def test_optional_threshold_undeclared_is_display_only(self):
        inp = EvalInput(NOW, CFG, {}, {}, ())
        j = catalog.judged_value(inp, "cms_cache_hit_ratio", "k", "l", 0.1, "10.0%", catalog._pct)
        self.assertEqual((j.status, j.judged, j.threshold), (Status.OK, False, None))

    def test_optional_threshold_declared_is_judged(self):
        cfg = with_thresholds(cms_cache_hit_ratio=config.Threshold(0.5, 0.2))
        j = catalog.judged_value(EvalInput(NOW, cfg, {}, {}, ()), "cms_cache_hit_ratio", "k", "l", 0.3, "30.0%",
                                 catalog._pct)
        self.assertEqual((j.status, j.judged), (Status.WARN, True))

    def test_required_threshold_undeclared_is_an_error(self):
        cfg = dataclasses.replace(CFG, thresholds={})
        with self.assertRaises(KeyError):
            catalog.judged_value(EvalInput(NOW, cfg, {}, {}, ()), "worker_exceeded", "k", "l", 1, "1", catalog._num)


class VisitorTest(unittest.TestCase):
    def test_healthy(self):
        js = judge({"festival.cloudflare": cf_result()})
        self.assertEqual({j.status for j in js.values()}, {Status.OK})
        self.assertEqual(set(js), {
            "visitor.worker.error_rate", "visitor.worker.exceeded", "visitor.worker.cpu_p99",
            "visitor.cms.5xx_rate", "visitor.cms.origin_p95", "visitor.media.4xx", "visitor.media.5xx"})
        self.assertEqual(js["visitor.worker.error_rate"].observed_at, datetime(2026, 6, 1, 11, 57, tzinfo=timezone.utc))

    def test_error_rate_boundary(self):
        js = judge({"festival.cloudflare": cf_result(worker={"errors": 10})})  # 10/1000 = 1.0%
        self.assertEqual(js["visitor.worker.error_rate"].status, Status.WARN)
        self.assertEqual(js["visitor.worker.error_rate"].value, "1.0% (10/1000)")
        js = judge({"festival.cloudflare": cf_result(worker={"errors": 50})})
        self.assertEqual(js["visitor.worker.error_rate"].status, Status.CRIT)

    def test_min_requests_is_ok_below_and_judged_at(self):
        below = judge({"festival.cloudflare": cf_result(worker={"requests": 49, "errors": 49})})
        self.assertEqual(below["visitor.worker.error_rate"].status, Status.OK)
        at = judge({"festival.cloudflare": cf_result(worker={"requests": 50, "errors": 50})})
        self.assertEqual(at["visitor.worker.error_rate"].status, Status.CRIT)
        cms = judge({"festival.cloudflare": cf_result(cms={"requests": 49, "status_5xx": 49})})
        self.assertEqual(cms["visitor.cms.5xx_rate"].status, Status.OK)

    def test_zero_requests(self):
        js = judge({"festival.cloudflare": cf_result(worker={"requests": 0})})
        self.assertEqual(js["visitor.worker.error_rate"].status, Status.OK)

    def test_counts_and_latencies(self):
        js = judge({"festival.cloudflare": cf_result(
            worker={"exceeded": 1, "cpu_p99_ms": 1000}, cms={"origin_p95_ms": 1500},
            media={"status_4xx": 100, "status_5xx": 1})})
        self.assertEqual(
            {k: js[k].status for k in ("visitor.worker.exceeded", "visitor.worker.cpu_p99", "visitor.cms.origin_p95",
                                       "visitor.media.4xx", "visitor.media.5xx")},
            {"visitor.worker.exceeded": Status.WARN, "visitor.worker.cpu_p99": Status.CRIT,
             "visitor.cms.origin_p95": Status.WARN, "visitor.media.4xx": Status.CRIT, "visitor.media.5xx": Status.WARN})

    def test_no_result_makes_no_items(self):
        self.assertEqual(judge({}), {})

    def test_uptime_excludes_idp_and_cms_db_monitors(self):
        r = result("monitor.uptimerobot", [
            Item("quota", "uptimerobot", Status.OK, {"used": 4, "limit": 50}),
            Item("monitor.1", "web", Status.OK, {"state": "up"}),
            Item("monitor.2", "api", Status.CRIT, {"state": "down"}),
            Item("monitor.3", "old", Status.WARN, {"state": "paused"}),
            Item("monitor.4", CFG.uptime_monitors["idp"], Status.CRIT, {"state": "down"}),
            Item("monitor.5", CFG.uptime_monitors["cms_db"], Status.CRIT, {"state": "down"})])
        js = judge({"monitor.uptimerobot": r})
        self.assertEqual({k: j.status for k, j in js.items()},
                         {"visitor.uptime.1": Status.OK, "visitor.uptime.2": Status.CRIT, "visitor.uptime.3": Status.WARN})
        self.assertEqual(js["visitor.uptime.2"].label, "api")

    def test_catalog_definitions(self):
        for g in CATALOG:
            self.assertEqual(g.category, catalog.Category.VISITOR)
            self.assertTrue(g.notify)
            self.assertNotIn("/accounts/", g.link or "")
        prefixes = [g.key_prefix for g in CATALOG]
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_no_excluded_topics(self):
        keys = " ".join(g.key_prefix for g in CATALOG)
        for word in ("billing", "dmarc", "tls", "mail", "quota"):
            self.assertNotIn(word, keys)


if __name__ == "__main__":
    unittest.main()
