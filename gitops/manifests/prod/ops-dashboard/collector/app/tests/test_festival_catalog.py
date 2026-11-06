import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from festival import catalog
from festival.catalog import CATALOG, EvalInput, Judgement
from model import Breakdown, Item, SourceResult, Status

PROD_TOML = Path(__file__).resolve().parents[2] / "dashboard.toml"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CFG = config.load(PROD_TOML).festival


def result(sid, items):
    return SourceResult(sid, Status.OK, NOW, NOW, tuple(items))


def cf_result(**over):
    vals = {
        "worker": {"requests": 1000, "errors": 0, "exceeded": 0, "cpu_p99_ms": 10, "window_end": "2026-06-01T11:57:00Z",
                   "by_status": []},
        "cms": {"requests": 1000, "status_5xx": 0, "origin_p95_ms": 100, "window_end": "2026-06-01T11:57:00Z",
                "by_status": []},
        "media": {"requests": 10, "status_4xx": 0, "status_5xx": 0, "window_end": "2026-06-01T11:57:00Z",
                  "by_status": []},
    }
    for k, patch in over.items():
        vals[k] = {**vals[k], **patch}
    return result("festival.cloudflare", [Item(k, k, Status.OK, v) for k, v in vals.items()])


def judge(results, cfg=CFG):
    inp = EvalInput(NOW, cfg, results, {}, ())
    return {j.key: j for g in CATALOG for j in g.judge(inp)}


def visitor_judge(results):
    inp = EvalInput(NOW, CFG, results, {}, ())
    return {j.key: j for g in catalog.VISITOR_GROUPS for j in g.judge(inp)}


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

    def test_without_crit_grade_never_returns_crit(self):
        above = config.Threshold(1, None)
        self.assertEqual([catalog.grade("worker_exceeded", above, v) for v in (0, 1, 1000)],
                         [Status.OK, Status.WARN, Status.WARN])
        below = config.Threshold(4, None)
        self.assertEqual([catalog.grade("tunnel_connections", below, v) for v in (4, 3, 0)],
                         [Status.OK, Status.WARN, Status.WARN])

    def test_threshold_text_without_crit_is_warn_only(self):
        self.assertEqual(catalog.threshold_text("worker_error_rate", config.Threshold(0.02, None), catalog._pct),
                         "注意 ≥ 2.0%")
        self.assertEqual(catalog.threshold_text("tunnel_connections", config.Threshold(12, None), catalog._num),
                         "注意 < 12")

    def test_chart_line_without_crit_has_no_crit(self):
        inp = EvalInput(NOW, with_thresholds(cms_cache_hit_ratio=config.Threshold(0.5, None)), {}, {}, ())
        j = catalog.judged_value(inp, "cms_cache_hit_ratio", "k", "l", 0.3, "30.0%", catalog._pct, mu="ratio")
        self.assertEqual((j.warn, j.crit, j.alert_on_warn, j.status), (0.5, None, True, Status.WARN))

    def test_alert_on_warn_follows_crit_omission(self):
        inp = EvalInput(NOW, CFG, {}, {}, ())
        self.assertTrue(catalog.judged_value(inp, "worker_error_rate", "k", "l", 0.0, "0", catalog._pct).alert_on_warn)
        self.assertFalse(catalog.judged_value(inp, "cms_5xx_rate", "k", "l", 0.0, "0", catalog._pct).alert_on_warn)

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
        js = visitor_judge({"festival.cloudflare": cf_result()})
        self.assertEqual({j.status for j in js.values()}, {Status.OK})
        self.assertEqual(set(js), {
            "visitor.worker.requests", "visitor.worker.error_rate", "visitor.worker.exceeded", "visitor.worker.cpu_p99",
            "visitor.cms.5xx_rate", "visitor.cms.origin_p95", "visitor.media.4xx", "visitor.media.5xx"})
        self.assertEqual(js["visitor.worker.error_rate"].observed_at, datetime(2026, 6, 1, 11, 57, tzinfo=timezone.utc))

    def test_error_rate_boundary(self):
        js = judge({"festival.cloudflare": cf_result(worker={"errors": 19})})
        self.assertEqual(js["visitor.worker.error_rate"].status, Status.OK)
        js = judge({"festival.cloudflare": cf_result(worker={"errors": 20})})  # 20/1000 = 2.0%
        self.assertEqual(js["visitor.worker.error_rate"].status, Status.WARN)
        self.assertEqual(js["visitor.worker.error_rate"].value, "2.0% (20/1000)")
        self.assertEqual(js["visitor.worker.error_rate"].threshold, "注意 ≥ 2.0%")
        self.assertTrue(js["visitor.worker.error_rate"].alert_on_warn)
        js = judge({"festival.cloudflare": cf_result(worker={"errors": 500})})
        self.assertEqual(js["visitor.worker.error_rate"].status, Status.WARN)  # 異常は宣言しない

    def test_min_requests_is_ok_below_and_judged_at(self):
        below = judge({"festival.cloudflare": cf_result(worker={"requests": 49, "errors": 49})})
        self.assertEqual(below["visitor.worker.error_rate"].status, Status.OK)
        at = judge({"festival.cloudflare": cf_result(worker={"requests": 50, "errors": 50})})
        self.assertEqual(at["visitor.worker.error_rate"].status, Status.WARN)
        cms = judge({"festival.cloudflare": cf_result(cms={"requests": 49, "status_5xx": 49})})
        self.assertEqual(cms["visitor.cms.5xx_rate"].status, Status.OK)

    def test_zero_requests(self):
        js = judge({"festival.cloudflare": cf_result(worker={"requests": 0})})
        self.assertEqual(js["visitor.worker.error_rate"].status, Status.OK)

    def test_counts_and_latencies(self):
        js = judge({"festival.cloudflare": cf_result(
            worker={"exceeded": 1, "cpu_p99_ms": 1000}, cms={"origin_p95_ms": 3000},
            media={"status_4xx": 100, "status_5xx": 1})})
        self.assertEqual(
            {k: js[k].status for k in ("visitor.worker.exceeded", "visitor.worker.cpu_p99", "visitor.cms.origin_p95",
                                       "visitor.media.4xx", "visitor.media.5xx")},
            {"visitor.worker.exceeded": Status.WARN, "visitor.worker.cpu_p99": Status.WARN,
             "visitor.cms.origin_p95": Status.WARN, "visitor.media.4xx": Status.CRIT, "visitor.media.5xx": Status.WARN})

    def test_no_result_makes_no_items(self):
        self.assertEqual(visitor_judge({}), {})

    def test_uptime_excludes_idp_and_cms_db_monitors(self):
        r = result("monitor.uptimerobot", [
            Item("quota", "uptimerobot", Status.OK, {"used": 4, "limit": 50}),
            Item("monitor.1", "web", Status.OK, {"state": "up"}),
            Item("monitor.2", "api", Status.CRIT, {"state": "down"}),
            Item("monitor.3", "old", Status.WARN, {"state": "paused"}),
            Item("monitor.4", CFG.uptime_monitors["idp"], Status.CRIT, {"state": "down"}),
            Item("monitor.5", CFG.uptime_monitors["cms_db"], Status.CRIT, {"state": "down"})])
        js = visitor_judge({"monitor.uptimerobot": r})
        self.assertEqual({k: j.status for k, j in js.items()},
                         {"visitor.uptime.1": Status.OK, "visitor.uptime.2": Status.CRIT, "visitor.uptime.3": Status.WARN})
        self.assertEqual(js["visitor.uptime.2"].label, "api")

    def test_catalog_definitions(self):
        for g in catalog.VISITOR_GROUPS:
            self.assertEqual(g.category, catalog.Category.VISITOR)
            self.assertTrue(g.notify)
            self.assertNotIn("/accounts/", g.link or "")
        prefixes = [g.key_prefix for g in CATALOG]
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_no_excluded_topics(self):
        keys = " ".join(g.key_prefix for g in CATALOG)
        for word in ("billing", "dmarc", "tls", "mail", "quota"):
            self.assertNotIn(word, keys)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw):
    return iso(NOW - timedelta(**kw))


# NOW = 2026-06-01 21:00 JST
EVENT = {"start_at": "2026-05-31T15:00:00Z", "end_at": "2026-06-01T09:00:00Z"}  # 6/1 00:00 JST 開始


def schedule(end="21:00", start="09:00", days=(EVENT,)):
    items = [Item("monitor", "m", Status.OK, {"start": start, "end": end})]
    items += [Item(f"event_day.{n}", f"d{n}", Status.OK, d) for n, d in enumerate(days)]
    return result("festival.schedule", items)


def camera(cid="1", **over):
    vals = {"name": "北", "lot": "A", "last_captured_at": ago(minutes=1), "device_received_at": None,
            "judge_last_succeeded_at": ago(minutes=1), "judge_last_failed_at": None, "judge_last_error": None,
            "judge_consecutive_failures": 0, "lot_status_updated_at": None, **over}
    return Item(f"camera.{cid}", vals["name"], Status.OK, vals)


def parking(*cams):
    return result("festival.parking", cams or [camera()])


class ParkingTest(unittest.TestCase):
    def run_(self, cams=None, sched=None, now=None, cfg=CFG):
        res = {"festival.parking": cams or parking(), "festival.schedule": sched or schedule()}
        inp = EvalInput(now or NOW, cfg, res, {}, ())
        return {j.key: j for g in catalog.FUNCTION_GROUPS for j in g.judge(inp)}

    def test_healthy(self):
        js = self.run_()
        self.assertEqual({k: j.status for k, j in js.items()},
                         {"function.parking.capture.1": Status.OK, "function.parking.judge.1": Status.OK})

    def test_capture_age_boundaries(self):
        s = lambda m: self.run_(parking(camera(last_captured_at=ago(minutes=m))))["function.parking.capture.1"].status
        self.assertEqual([s(9), s(10), s(15)], [Status.OK, Status.WARN, Status.CRIT])

    def test_capture_never_is_crit(self):
        self.assertEqual(self.run_(parking(camera(last_captured_at=None)))["function.parking.capture.1"].status,
                         Status.CRIT)

    PARKING_JUDGE = dict(parking_judge_failures=config.Threshold(1, 5),
                         parking_judge_success_age=config.Threshold(timedelta(minutes=10), timedelta(minutes=20)))

    def test_judge_is_display_only_when_neither_threshold_is_declared(self):
        j = self.run_(parking(camera(judge_consecutive_failures=9, judge_last_succeeded_at=None)))[
            "function.parking.judge.1"]
        self.assertEqual((j.status, j.judged, j.threshold, j.alert_on_warn), (Status.OK, False, None, False))
        self.assertEqual(j.value, "連続失敗 9 回 / 成功なし")

    def test_judge_worse_of_failures_and_age(self):
        cfg = with_thresholds(**self.PARKING_JUDGE)
        j = lambda **kw: self.run_(parking(camera(**kw)), cfg=cfg)["function.parking.judge.1"].status
        self.assertEqual(j(judge_consecutive_failures=1), Status.WARN)
        self.assertEqual(j(judge_consecutive_failures=5), Status.CRIT)
        self.assertEqual(j(judge_last_succeeded_at=ago(minutes=10)), Status.WARN)
        self.assertEqual(j(judge_last_succeeded_at=ago(minutes=20), judge_consecutive_failures=1), Status.CRIT)
        self.assertEqual(j(judge_last_succeeded_at=None), Status.CRIT)

    def test_judge_uses_only_the_declared_threshold(self):
        cfg = with_thresholds(parking_judge_failures=config.Threshold(1, 5))
        j = self.run_(parking(camera(judge_consecutive_failures=1, judge_last_succeeded_at=None)),
                      cfg=cfg)["function.parking.judge.1"]
        self.assertEqual((j.status, j.judged, j.threshold), (Status.WARN, True, "失敗 注意 ≥ 1 / 異常 ≥ 5"))
        cfg = with_thresholds(parking_judge_success_age=config.Threshold(timedelta(minutes=10), None))
        j = self.run_(parking(camera(judge_consecutive_failures=9, judge_last_succeeded_at=None)),
                      cfg=cfg)["function.parking.judge.1"]
        self.assertEqual((j.status, j.judged, j.alert_on_warn, j.threshold), (Status.WARN, True, True, "経過 注意 ≥ 10 分"))

    def test_out_of_hours_boundary_end_is_inclusive(self):
        at = lambda h, m, s=0: self.run_(now=datetime(2026, 6, 1, h, m, s, tzinfo=timezone(timedelta(hours=9)))
                                         )["function.parking.capture.1"].status
        # カメラの時刻は NOW 基準のため、判定の有無 (EMPTY かどうか) だけを見る
        self.assertNotEqual(at(21, 0), Status.EMPTY)
        self.assertEqual(at(21, 0, 1), Status.EMPTY)
        self.assertNotEqual(at(9, 0), Status.EMPTY)
        self.assertEqual(at(8, 59, 59), Status.EMPTY)

    def test_not_event_day_is_out_of_hours(self):
        other = {"start_at": "2026-06-01T15:00:00Z", "end_at": "2026-06-02T09:00:00Z"}  # 6/2 JST
        js = self.run_(sched=schedule(days=(other,)))
        self.assertEqual({j.status for j in js.values()}, {Status.EMPTY})

    def test_event_day_uses_jst_date_of_start(self):
        # 6/1 14:59Z = 6/1 23:59 JST は 6/1 の開催日
        day = {"start_at": "2026-06-01T14:59:00Z", "end_at": "2026-06-01T15:30:00Z"}
        js = self.run_(sched=schedule(days=(day,)))
        self.assertNotEqual(js["function.parking.capture.1"].status, Status.EMPTY)

    def test_schedule_missing_is_error(self):
        inp = EvalInput(NOW, CFG, {"festival.parking": parking()}, {}, ())
        js = {j.key: j for g in catalog.FUNCTION_GROUPS for j in g.judge(inp)}
        self.assertEqual({j.status for j in js.values()}, {Status.ERROR})

    def test_no_parking_result_makes_no_items(self):
        self.assertEqual(self.run_(cams=result("festival.parking", [])), {})


def fjudge(results, cfg=CFG):
    inp = EvalInput(NOW, cfg, results, {}, ())
    return {j.key: j for g in catalog.FUNCTION_GROUPS for j in g.judge(inp)}


def signage(prod):
    return result("festival.signage", [Item("connections", "c", Status.OK,
                                            {"prod": prod, "dev": 0, "preview": 0, "unknown": 0, "at": ago(seconds=10)})])


class SignageTest(unittest.TestCase):
    def test_display_only_regardless_of_count(self):
        for prod in (0, 3, 12):
            j = fjudge({"festival.signage": signage(prod)})["function.signage.prod"]
            self.assertEqual((j.status, j.judged, j.value, j.threshold, j.warn, j.crit, j.metric),
                             (Status.OK, False, f"{prod} 台", None, None, None, prod))


def quake(cron=..., channels=None, unreachable=False, discord=None):
    items = []
    if cron is not ...:
        items.append(Item("cron", "cron", Status.OK, cron))
    for n, c in (channels or {"p2p": {}}).items():
        items.append(Item(f"channel.{n}", n, Status.OK,
                          {"connected": True, "receiving": True, "last_received_at": ago(seconds=5), "test": False, **c}))
    items.append(Item("unreachable", "u", Status.OK, {"value": unreachable}))
    items.append(Item("discord", "d", Status.OK, {
        "configured": True, "disabled": False, "last_success_at": ago(hours=1), "last_failure_at": None,
        "last_failure": None, **(discord or {})}))
    return result("festival.quake", items)


def cron(minutes=1, in_period=True):
    return {"last_run_at": ago(minutes=minutes), "in_period": in_period}


class QuakeTest(unittest.TestCase):
    def test_cron_age_and_period(self):
        s = lambda **kw: fjudge({"festival.quake": quake(cron=cron(**kw))})["function.quake.cron"].status
        self.assertEqual([s(minutes=6), s(minutes=7), s(minutes=12)], [Status.OK, Status.WARN, Status.CRIT])
        self.assertEqual(s(minutes=60, in_period=False), Status.EMPTY)

    def test_cron_missing_info_is_error(self):
        self.assertEqual(fjudge({"festival.quake": quake()})["function.quake.cron"].status, Status.ERROR)

    def test_cron_never_run_in_period_is_crit(self):
        j = fjudge({"festival.quake": quake(cron={"last_run_at": None, "in_period": True})})
        self.assertEqual(j["function.quake.cron"].status, Status.CRIT)

    def test_channels(self):
        chans = {"p2p": {}, "jma": {"receiving": False}}
        js = fjudge({"festival.quake": quake(cron=cron(), channels=chans)})
        self.assertEqual((js["function.quake.channel.p2p"].status, js["function.quake.channel.jma"].status),
                         (Status.OK, Status.WARN))
        js = fjudge({"festival.quake": quake(cron=cron(), channels=chans, unreachable=True)})
        self.assertEqual({js[k].status for k in js if ".channel." in k}, {Status.CRIT})
        js = fjudge({"festival.quake": quake(cron=cron(in_period=False), channels=chans, unreachable=True)})
        self.assertEqual({js[k].status for k in js if ".channel." in k}, {Status.EMPTY})

    def test_discord(self):
        d = lambda **kw: fjudge({"festival.quake": quake(cron=cron(), discord=kw)})["function.quake.discord"].status
        self.assertEqual(d(), Status.OK)
        self.assertEqual(d(disabled=True), Status.CRIT)
        self.assertEqual(d(configured=False), Status.WARN)
        self.assertEqual(d(last_failure_at=ago(minutes=1)), Status.WARN)
        self.assertEqual(d(last_failure_at=ago(hours=2)), Status.OK)


class StampBusTest(unittest.TestCase):
    def test_stamp(self):
        r = result("festival.stamp", [Item("stamp", "s", Status.OK, {
            "failed": 1, "rate_limited": 100, "d1_failed": 0, "window_end": "2026-06-01T11:57:00Z"})])
        js = fjudge({"festival.stamp": r})
        self.assertEqual({k: (j.status, j.judged) for k, j in js.items()}, {
            "function.stamp.failed": (Status.OK, False), "function.stamp.rate_limited": (Status.OK, False),
            "function.stamp.d1_failed": (Status.OK, False)})
        cfg = with_thresholds(stamp_failed=config.Threshold(1, 10), stamp_rate_limited=config.Threshold(10, 100))
        self.assertEqual({k: j.status for k, j in fjudge({"festival.stamp": r}, cfg).items()}, {
            "function.stamp.failed": Status.WARN, "function.stamp.rate_limited": Status.CRIT,
            "function.stamp.d1_failed": Status.OK})

    def test_bus_ignores_event_days(self):
        feeds = [Item("feed.trip_update", "a", Status.OK, {"header_timestamp": ago(minutes=1)}),
                 Item("feed.vehicle", "b", Status.OK, {"header_timestamp": ago(minutes=5)})]
        js = fjudge({"festival.bus": result("festival.bus", feeds)})
        self.assertEqual((js["function.bus.trip_update"].status, js["function.bus.vehicle"].status),
                         (Status.OK, Status.CRIT))

    def test_bus_failed_feed_is_stale_with_note(self):
        feeds = [Item("feed.vehicle", "b", Status.STALE, {"header_timestamp": None}, "HttpError: x")]
        j = fjudge({"festival.bus": result("festival.bus", feeds)})["function.bus.vehicle"]
        self.assertEqual((j.status, j.note), (Status.STALE, "HttpError: x"))


class UniqueKeysTest(unittest.TestCase):
    def test_prefixes_unique_and_function_category(self):
        prefixes = [g.key_prefix for g in CATALOG]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        for g in catalog.FUNCTION_GROUPS:
            self.assertEqual(g.category, catalog.Category.FUNCTION)


def wl_cms(**over):
    v = {"replicas": 2, "ready": 2, "restarts_recent": 0, "oom_recent": False, "crashloop": False,
         "cpu_ratio_max": 0.1, "throttle_ratio_max": 0.0, "by_pod": [], **over}
    return Item("cms", "CMS", Status.OK, v)


def wl_db(pod, role="primary", **over):
    v = {"role": role, "cpu_ratio": 0.1, "memory_rss_ratio": 0.2, "restarts_recent": 0, "oom_recent": False,
         "crashloop": False, **over}
    return Item(f"db.{pod}", pod, Status.OK, v)


def cnpg_db(pod, role="primary", **over):
    v = {"role": role, "backends": 10, "max_connections": 100.0,
         "replication_lag_s": None if role == "primary" else 1.0,
         "unanalyzed_tables": 0 if role == "primary" else None, **over}
    return Item(f"db.{pod}", pod, Status.OK, v)


def node(name, **over):
    v = {"ready": True, "cpu_ratio": 0.1, "memory_ratio": 0.2, "disk_ratio": 0.3, "addresses_count": 2, **over}
    return Item(f"node.{name}", name, Status.OK, v)


class CapacityTest(unittest.TestCase):
    def j(self, results, cfg=CFG, **kw):
        inp = EvalInput(NOW, cfg, results, {}, (), **kw)
        return {j.key: j for g in catalog.CAPACITY_GROUPS for j in g.judge(inp)}

    def test_healthy_keys(self):
        js = self.j({
            "festival.workloads": result("festival.workloads", [
                wl_cms(), wl_db("db-1"), wl_db("db-2", "replica"),
                Item("cloudflared", "cloudflared", Status.OK, {"pods": 1, "cpu_ratio_max": 0.1,
                                                              "throttle_ratio_max": 0.0})]),
            "festival.cnpg": result("festival.cnpg", [cnpg_db("db-1"), cnpg_db("db-2", "replica")]),
            "festival.cloudflare": result("festival.cloudflare", [
                Item("cms", "cms", Status.OK, {"requests": 3000, "window_end": "2026-06-01T11:57:00Z"}),
                Item("media", "media", Status.OK, {"requests": 600}),
                Item("cms_cache", "c", Status.OK, {"lookups": 0, "hits": 0, "window_end": "2026-06-01T11:57:00Z"})]),
            "festival.nodes": result("festival.nodes", [node("n1")]),
            "connect.tunnel": result("connect.tunnel", [Item("tunnel", "tunnel", Status.OK,
                                                            {"state": "healthy", "connections": 12})])})
        self.assertEqual(set(js), {
            "capacity.cms.ready", "capacity.cms.restarts", "capacity.cms.cpu", "capacity.db.primary_cpu",
            "capacity.db.connections", "capacity.db.backends.db-1", "capacity.db.backends.db-2",
            "capacity.db.memory.db-1", "capacity.db.memory.db-2", "capacity.db.restarts", "capacity.db.unanalyzed",
            "capacity.db.lag.db-2", "capacity.cms.throttle", "capacity.cms.rps", "capacity.cms.cache_hit",
            "capacity.cloudflared.cpu", "capacity.cloudflared.throttle", "capacity.node.n1.ready",
            "capacity.node.n1.cpu", "capacity.node.n1.memory", "capacity.node.n1.disk", "capacity.tunnel"})
        self.assertEqual({j.status for j in js.values()}, {Status.OK})
        self.assertEqual(js["capacity.cms.rps"].value, "8.00 req/s")  # (3000-600)/300、メディアを除く
        self.assertEqual(js["capacity.db.connections"].value, "10.0% (10/100)")

    def test_no_result_makes_no_items(self):
        self.assertEqual(self.j({}), {})

    def test_cms_ready_below_is_exclusive(self):
        w = lambda **o: self.j({"festival.workloads": result("festival.workloads", [wl_cms(**o)])})["capacity.cms.ready"]
        self.assertEqual(w(ready=2).status, Status.OK)
        self.assertEqual(w(ready=1).status, Status.WARN)  # 0.5 は異常の境界 (未満) に達しない
        self.assertEqual(w(ready=0).status, Status.CRIT)

    def test_only_crashloop_is_crit_for_restarts(self):
        w = lambda **o: self.j({"festival.workloads": result("festival.workloads", [wl_cms(**o), wl_db("db-1", **o)])})
        for kw in ({"restarts_recent": 1}, {"oom_recent": True}, {"restarts_recent": 3, "oom_recent": True}):
            for key in ("capacity.cms.restarts", "capacity.db.restarts"):
                self.assertEqual(w(**kw)[key].status, Status.OK, (kw, key))
        for key in ("capacity.cms.restarts", "capacity.db.restarts"):
            self.assertEqual(w(crashloop=True)[key].status, Status.CRIT, key)
        self.assertEqual(w(oom_recent=True)["capacity.cms.restarts"].value, "OOM")
        self.assertEqual(w(restarts_recent=2)["capacity.db.restarts"].value, "db-1 (再起動 2 回)")
        self.assertEqual(w(crashloop=True)["capacity.db.restarts"].value, "db-1 (CrashLoop)")
        self.assertEqual(w()["capacity.db.restarts"].value, "なし")

    def test_throttle_and_cpu_none_carry_note(self):
        cms = Item("cms", "CMS", Status.OK, {"replicas": 1, "ready": 1, "restarts_recent": 0, "oom_recent": False,
                                             "crashloop": False, "cpu_ratio_max": None, "throttle_ratio_max": None},
                   "メトリクス欠落")
        js = self.j({"festival.workloads": result("festival.workloads", [cms])})
        self.assertEqual((js["capacity.cms.cpu"].status, js["capacity.cms.cpu"].value, js["capacity.cms.cpu"].note),
                         (Status.OK, "—", "メトリクス欠落"))
        self.assertEqual(js["capacity.cms.throttle"].note, "メトリクス欠落")

    def test_db_primary_and_replica_selection(self):
        js = self.j({"festival.workloads": result("festival.workloads", [
                        wl_db("p", cpu_ratio=0.95), wl_db("r", "replica", cpu_ratio=0.99)]),
                     "festival.cnpg": result("festival.cnpg", [
                         cnpg_db("p", backends=70, unanalyzed_tables=10), cnpg_db("r", "replica", replication_lag_s=10)])},
                    with_thresholds(db_unanalyzed_tables=config.Threshold(1, 10),
                                    db_replica_lag_s=config.Threshold(10, 60)))
        self.assertEqual(js["capacity.db.primary_cpu"].status, Status.CRIT)  # replica の 0.99 は混ざらない
        self.assertEqual(js["capacity.db.connections"].status, Status.CRIT)  # 0.7
        self.assertEqual(js["capacity.db.unanalyzed"].status, Status.CRIT)
        self.assertEqual(js["capacity.db.lag.r"].status, Status.WARN)
        self.assertNotIn("capacity.db.lag.p", js)

    def test_rps_excludes_media(self):
        cf = lambda cms, media: self.j({"festival.cloudflare": result("festival.cloudflare", [
            Item("cms", "cms", Status.OK, {"requests": cms, "window_end": "2026-06-01T11:57:00Z"}),
            Item("media", "media", Status.OK, {"requests": media})])})["capacity.cms.rps"]
        self.assertEqual(cf(30000, 6000).metric, 80.0)  # (30000-6000)/300
        self.assertEqual(cf(30000, 30000).metric, 0.0)  # 全部メディアなら 0

    def test_rps_display_only_until_declared(self):
        cf = {"festival.cloudflare": result("festival.cloudflare", [
            Item("cms", "cms", Status.OK, {"requests": 30000, "window_end": "2026-06-01T11:57:00Z"}),
            Item("media", "media", Status.OK, {"requests": 6000})])}
        self.assertEqual((self.j(cf)["capacity.cms.rps"].judged, self.j(cf)["capacity.cms.rps"].status),
                         (False, Status.OK))
        cfg = with_thresholds(cms_rps=config.Threshold(80, 115))
        self.assertEqual(self.j(cf, cfg)["capacity.cms.rps"].status, Status.WARN)  # 80 req/s ちょうど

    def test_cache_hit_display_only_until_declared(self):
        cache = {"festival.cloudflare": result("festival.cloudflare", [
            Item("cms_cache", "c", Status.OK, {"lookups": 100, "hits": 30, "window_end": "2026-06-01T11:57:00Z"})])}
        j = self.j(cache)["capacity.cms.cache_hit"]
        self.assertEqual((j.judged, j.status, j.value), (False, Status.OK, "30.0% (30/100)"))
        cfg = with_thresholds(cms_cache_hit_ratio=config.Threshold(0.5, 0.2))
        self.assertEqual(self.j(cache, cfg)["capacity.cms.cache_hit"].status, Status.WARN)

    def test_cache_hit_zero_lookups_is_ok(self):
        cfg = with_thresholds(cms_cache_hit_ratio=config.Threshold(0.5, 0.2))
        j = self.j({"festival.cloudflare": result("festival.cloudflare", [
            Item("cms_cache", "c", Status.OK, {"lookups": 0, "hits": 0, "window_end": "2026-06-01T11:57:00Z"})])},
            cfg)["capacity.cms.cache_hit"]
        self.assertEqual((j.status, j.value, j.judged), (Status.OK, "照会 0 件", True))

    def test_node_rules(self):
        js = self.j({"festival.nodes": result("festival.nodes", [
            node("a", ready=False, cpu_ratio=0.75, memory_ratio=0.95, disk_ratio=None)])})
        self.assertEqual(js["capacity.node.a.ready"].status, Status.CRIT)
        self.assertEqual(js["capacity.node.a.cpu"].status, Status.WARN)
        self.assertEqual((js["capacity.node.a.memory"].status, js["capacity.node.a.memory"].judged), (Status.OK, False))
        self.assertEqual((js["capacity.node.a.disk"].status, js["capacity.node.a.disk"].value), (Status.OK, "—"))

    def test_tunnel_takes_worse_of_state_and_connections(self):
        t = lambda status, conns: self.j({"connect.tunnel": result("connect.tunnel", [
            Item("tunnel", "tunnel", status, {"state": "x", "connections": conns})])})["capacity.tunnel"].status
        self.assertEqual(t(Status.OK, 12), Status.OK)
        self.assertEqual(t(Status.OK, 11), Status.WARN)
        self.assertEqual(t(Status.OK, 8), Status.WARN)
        self.assertEqual(t(Status.OK, 7), Status.CRIT)
        self.assertEqual(t(Status.CRIT, 12), Status.CRIT)


class InfraTest(unittest.TestCase):
    def j(self, results=None, falco=None, nets=(), state=None, **kw):
        inp = EvalInput(NOW, CFG, results or {}, falco or {}, nets, state, **kw)
        return {j.key: j for g in catalog.INFRA_GROUPS for j in g.judge(inp)}

    def test_incidents(self):
        gh = lambda *items: self.j({"ci.github": result("ci.github", items)})["infra.incidents"]
        inc = lambda n, label, st: Item(f"incident.{label}.{n}", f"題{n}", st, {"label": label})
        self.assertEqual((gh().status, gh().value), (Status.OK, "異常 0 件 / 注意 0 件"))
        self.assertEqual(gh(inc(1, "infra-alert", Status.WARN)).status, Status.WARN)
        j = gh(inc(1, "infra-alert", Status.WARN), inc(2, "dr-incident", Status.CRIT))
        self.assertEqual((j.status, j.value), (Status.CRIT, "異常 1 件 / 注意 1 件: 題2 / 題1"))

    def test_falco_priorities(self):
        f = lambda c: self.j(falco=c)["infra.falco"]
        self.assertEqual((f({}).status, f({}).value), (Status.OK, "0 件"))
        self.assertEqual(f({"notice": 9, "warning": 0}).status, Status.OK)
        self.assertEqual(f({"warning": 2}).status, Status.WARN)
        self.assertEqual(f({"error": 1}).status, Status.WARN)  # warning より重い
        self.assertEqual(f({"critical": 1, "warning": 2}).status, Status.CRIT)
        self.assertEqual(f({"emergency": 1}).status, Status.CRIT)
        self.assertEqual(f({"critical": 1, "warning": 2}).value, "critical 1 件 / warning 2 件")

    def test_idp_and_cms_db_by_label(self):
        r = result("monitor.uptimerobot", [
            Item("monitor.1", CFG.uptime_monitors["idp"], Status.CRIT, {"state": "down"}),
            Item("monitor.2", CFG.uptime_monitors["cms_db"], Status.STALE, {"state": "pending"})])
        js = self.j({"monitor.uptimerobot": r})
        self.assertEqual((js["infra.idp"].status, js["infra.idp"].value), (Status.CRIT, "down"))
        self.assertEqual(js["infra.cms_db"].status, Status.OK)
        js = self.j({"monitor.uptimerobot": result("monitor.uptimerobot", [])})
        self.assertEqual(js["infra.idp"].status, Status.WARN)

    def test_healthchecks_skip_quota(self):
        r = result("monitor.healthchecks", [
            Item("quota", "healthchecks", Status.OK, {}),
            Item("check.abc", "backup", Status.CRIT, {"state": "down"}),
            Item("check.def", "new", Status.STALE, {"state": "new"})])
        js = self.j({"monitor.healthchecks": r})
        self.assertEqual({k: j.status for k, j in js.items() if k.startswith("infra.healthchecks.")},
                         {"infra.healthchecks.abc": Status.CRIT, "infra.healthchecks.def": Status.OK})

    def test_fail2ban_counts_internal_only_and_hides_ip(self):
        import ipaddress
        nets = (ipaddress.ip_network("10.0.0.5/32"), ipaddress.ip_network("10.42.0.0/24"))
        bans = result("mail.fail2ban", [
            Item("ban.sshd.10.42.0.7", "sshd", Status.OK, {"ip": "10.42.0.7"}),
            Item("ban.sshd.10.0.0.5", "sshd", Status.OK, {"ip": "10.0.0.5"}),
            Item("ban.sshd.203.0.113.9", "sshd", Status.OK, {"ip": "203.0.113.9"}),
            Item("ban.sshd.2001:db8::1", "sshd", Status.OK, {"ip": "2001:db8::1"})])
        nodes = result("festival.nodes", [])
        j = self.j({"mail.fail2ban": bans, "festival.nodes": nodes}, nets=nets)["infra.fail2ban.internal"]
        self.assertEqual((j.status, j.value), (Status.CRIT, "2 件"))
        j = self.j({"mail.fail2ban": bans, "festival.nodes": nodes})["infra.fail2ban.internal"]
        self.assertEqual(j.status, Status.OK)
        self.assertNotIn("infra.fail2ban.internal", self.j({"mail.fail2ban": bans}))

    def test_notifier_and_heartbeat_state(self):
        from model import FestivalStateRow, NotifierState
        t0, t1 = NOW, NOW.replace(minute=5)
        st = lambda **o: FestivalStateRow(NOW, None, NotifierState(**{
            "last_success_at": None, "last_failure_at": None, "last_failure": None, "not_before": None,
            "halted": False, **o.pop("n", {})}), **{"heartbeat_ok_at": None, "heartbeat_failed_at": None,
                                                    "heartbeat_failure": None, **o})
        self.assertNotIn("infra.notifier", self.j())  # state なしは項目を作らない
        js = self.j(state=st())
        self.assertEqual((js["infra.notifier"].status, js["infra.heartbeat"].status), (Status.OK, Status.OK))
        js = self.j(state=st(), notifier_configured=False, heartbeat_configured=False)
        self.assertEqual((js["infra.notifier"].value, js["infra.heartbeat"].value), ("未設定", "未設定"))
        js = self.j(state=st(n={"last_success_at": t0, "last_failure_at": t1, "last_failure": "500"},
                             heartbeat_ok_at=t0, heartbeat_failed_at=t1, heartbeat_failure="timeout"))
        self.assertEqual((js["infra.notifier"].status, js["infra.heartbeat"].status), (Status.WARN, Status.WARN))
        js = self.j(state=st(n={"last_success_at": t1, "last_failure_at": t0, "last_failure": "500"},
                             heartbeat_ok_at=t1, heartbeat_failed_at=t0))
        self.assertEqual((js["infra.notifier"].status, js["infra.heartbeat"].status), (Status.OK, Status.OK))
        js = self.j(state=st(n={"halted": True, "last_failure": "401"}))
        self.assertEqual((js["infra.notifier"].status, js["infra.notifier"].value), (Status.WARN, "停止中 (401)"))

    def test_notifier_heartbeat_are_not_notified(self):
        by = {g.key_prefix: g for g in catalog.INFRA_GROUPS}
        self.assertFalse(by["infra.notifier"].notify)
        self.assertFalse(by["infra.heartbeat"].notify)
        self.assertTrue(by["infra.falco"].notify)


class AllKeysTest(unittest.TestCase):
    def test_every_key_unique_and_prefixed(self):
        import ipaddress
        from model import FestivalStateRow, NotifierState
        res = {
            "festival.workloads": result("festival.workloads", [wl_cms(), wl_db("p"), wl_db("r", "replica")]),
            "festival.cnpg": result("festival.cnpg", [cnpg_db("p"), cnpg_db("r", "replica")]),
            "festival.nodes": result("festival.nodes", [node("a"), node("b")]),
            "monitor.uptimerobot": result("monitor.uptimerobot", [
                Item("monitor.1", CFG.uptime_monitors["idp"], Status.OK, {"state": "up"}),
                Item("monitor.2", CFG.uptime_monitors["cms_db"], Status.OK, {"state": "up"}),
                Item("monitor.3", "web", Status.OK, {"state": "up"})]),
            "monitor.healthchecks": result("monitor.healthchecks", [Item("check.x", "x", Status.OK, {"state": "up"})]),
            "mail.fail2ban": result("mail.fail2ban", []),
            "ci.github": result("ci.github", [])}
        st = FestivalStateRow(NOW, None, NotifierState(None, None, None, None, False), None, None, None)
        inp = EvalInput(NOW, CFG, res, {}, (ipaddress.ip_network("10.0.0.0/8"),), st)
        for g in CATALOG:
            for j in g.judge(inp):
                self.assertTrue(j.key.startswith(g.key_prefix), j.key)
                if g.category in (catalog.Category.CAPACITY, catalog.Category.INFRA):
                    self.assertTrue(j.key.startswith(g.category.value + "."), j.key)
        keys = [j.key for g in CATALOG for j in g.judge(inp)]
        self.assertEqual(len(keys), len(set(keys)))


def _sec(v):
    return v.total_seconds() if isinstance(v, timedelta) else v


def full_results(cache=None):
    """グラフの対象の全項目 (カメラ・DB instance・ノードを各 1 つ以上) を作れる入力。"""
    return {
        "festival.cloudflare": result("festival.cloudflare", [
            *cf_result(cms={"requests": 3000}, media={"requests": 600}).items,
            Item("cms_cache", "c", Status.OK, {"lookups": 100, "hits": 30, "window_end": "2026-06-01T11:57:00Z"} if cache is None else cache)]),
        "festival.parking": parking(), "festival.schedule": schedule(),
        "festival.signage": signage(9),
        "festival.stamp": result("festival.stamp", [Item("stamp", "s", Status.OK, {
            "failed": 0, "rate_limited": 0, "d1_failed": 0, "window_end": "2026-06-01T11:57:00Z"})]),
        "festival.bus": result("festival.bus", [Item("feed.vehicle", "b", Status.OK, {"header_timestamp": ago(minutes=1)})]),
        "festival.quake": quake(cron=cron()),
        "festival.workloads": result("festival.workloads", [
            wl_cms(), wl_db("p"), wl_db("r", "replica"),
            Item("cloudflared", "cloudflared", Status.OK, {"pods": 1, "cpu_ratio_max": 0.1, "throttle_ratio_max": 0.0})]),
        "festival.cnpg": result("festival.cnpg", [cnpg_db("p"), cnpg_db("r", "replica")]),
        "festival.nodes": result("festival.nodes", [node("a")]),
        "connect.tunnel": result("connect.tunnel", [Item("tunnel", "tunnel", Status.OK,
                                                        {"state": "healthy", "connections": 4})])}


# 設計「グラフの対象」の表。キー -> (単位, 閾値名)
GRAPH = {
    "visitor.worker.requests": ("count", None),
    "visitor.worker.error_rate": ("ratio", "worker_error_rate"),
    "visitor.worker.exceeded": ("count", "worker_exceeded"),
    "visitor.worker.cpu_p99": ("ms", "worker_cpu_p99_ms"),
    "visitor.cms.5xx_rate": ("ratio", "cms_5xx_rate"),
    "visitor.cms.origin_p95": ("ms", "cms_origin_p95_ms"),
    "visitor.media.4xx": ("count", "media_4xx"),
    "visitor.media.5xx": ("count", "media_5xx"),
    "function.parking.capture.1": ("seconds", "parking_capture_age"),
    "function.parking.judge.1": ("count", "parking_judge_failures"),
    "function.signage.prod": ("count", None),
    "function.stamp.failed": ("count", "stamp_failed"),
    "function.stamp.d1_failed": ("count", "stamp_d1_failed"),
    "function.stamp.rate_limited": ("count", "stamp_rate_limited"),
    "function.bus.vehicle": ("seconds", "bus_feed_age"),
    "capacity.cms.cpu": ("ratio", "pod_cpu_ratio"),
    "capacity.cms.throttle": ("ratio", "cpu_throttle_ratio"),
    "capacity.cms.rps": ("rps", "cms_rps"),
    "capacity.cms.cache_hit": ("ratio", "cms_cache_hit_ratio"),
    "capacity.db.primary_cpu": ("ratio", "pod_cpu_ratio"),
    "capacity.db.connections": ("ratio", "db_connections_ratio"),
    "capacity.db.backends.p": ("count", "db_backends"),
    "capacity.db.backends.r": ("count", "db_backends"),
    "capacity.db.memory.p": ("ratio", "db_memory_rss_ratio"),
    "capacity.db.memory.r": ("ratio", "db_memory_rss_ratio"),
    "capacity.db.unanalyzed": ("count", "db_unanalyzed_tables"),
    "capacity.db.lag.r": ("seconds", "db_replica_lag_s"),
    "capacity.cloudflared.cpu": ("ratio", "pod_cpu_ratio"),
    "capacity.cloudflared.throttle": ("ratio", "cpu_throttle_ratio"),
    "capacity.node.a.cpu": ("ratio", "node_cpu_ratio"),
    "capacity.node.a.memory": ("ratio", "node_memory_ratio"),
    "capacity.node.a.disk": ("ratio", "node_disk_ratio"),
    "capacity.tunnel": ("count", "tunnel_connections"),
}


class ChartMetaTest(unittest.TestCase):
    def test_graph_items_match_the_design_table(self):
        js = judge(full_results())
        self.assertEqual({k for k, j in js.items() if j.unit is not None}, set(GRAPH))
        for key, (unit, name) in GRAPH.items():
            j = js[key]
            self.assertEqual(j.unit, unit, key)
            self.assertIsNotNone(j.metric, key)
            th = CFG.thresholds.get(name) if name else None
            want = (None, None) if th is None else (_sec(th.warn), _sec(th.crit))
            self.assertEqual((j.warn, j.crit), want, key)

    def test_non_graph_items_have_no_chart_fields(self):
        for key, j in judge(full_results()).items():
            if key not in GRAPH:
                self.assertEqual((j.metric, j.unit, j.warn, j.crit), (None, None, None, None), key)

    def test_duration_thresholds_are_seconds(self):
        j = judge(full_results())["function.parking.capture.1"]
        self.assertEqual((j.metric, j.warn, j.crit), (60.0, 600.0, 900.0))
        b = judge(full_results())["function.bus.vehicle"]
        self.assertEqual((b.metric, b.warn, b.crit), (60.0, 120.0, 300.0))

    def test_metric_values(self):
        js = judge(full_results())
        self.assertEqual(js["visitor.worker.requests"].metric, 1000)
        self.assertEqual(js["visitor.worker.error_rate"].metric, 0.0)
        self.assertEqual(js["capacity.cms.cache_hit"].metric, 0.3)
        self.assertEqual(js["capacity.cms.rps"].metric, 8.0)
        self.assertEqual(js["capacity.db.connections"].metric, 0.1)
        self.assertEqual(js["capacity.db.lag.r"].metric, 1.0)
        self.assertEqual(js["function.parking.judge.1"].metric, 0)

    def test_error_rate_metric_is_set_below_min_requests(self):
        j = judge({"festival.cloudflare": cf_result(worker={"requests": 10, "errors": 5})})["visitor.worker.error_rate"]
        self.assertEqual((j.status, j.metric), (Status.OK, 0.5))

    def test_unavailable_values_have_no_metric(self):
        zero = judge(full_results({"lookups": 0, "hits": 0, "window_end": "2026-06-01T11:57:00Z"}))
        self.assertEqual((zero["capacity.cms.cache_hit"].metric, zero["capacity.cms.cache_hit"].unit), (None, "ratio"))
        never = judge(full_results() | {"festival.parking": parking(camera(last_captured_at=None))})
        self.assertEqual((never["function.parking.capture.1"].metric, never["function.parking.capture.1"].unit),
                         (None, "seconds"))

    def test_parking_out_of_hours_has_no_metric(self):
        js = judge(full_results() | {"festival.schedule": schedule(days=())})
        for k in ("function.parking.capture.1", "function.parking.judge.1"):
            self.assertEqual((js[k].status, js[k].metric), (Status.EMPTY, None))

    def test_cache_hit_line_only_when_declared(self):
        self.assertEqual(judge(full_results())["capacity.cms.cache_hit"].warn, None)
        cfg = with_thresholds(cms_cache_hit_ratio=config.Threshold(0.5, 0.2))
        j = judge(full_results(), cfg)["capacity.cms.cache_hit"]
        self.assertEqual((j.warn, j.crit), (0.5, 0.2))

    def test_worker_requests_is_display_only(self):
        j = judge({"festival.cloudflare": cf_result()})["visitor.worker.requests"]
        self.assertEqual((j.label, j.value, j.status, j.judged, j.threshold, j.warn, j.crit),
                         ("Worker 要求数", "1000 件", Status.OK, False, None, None, None))
        self.assertEqual(j.observed_at, datetime(2026, 6, 1, 11, 57, tzinfo=timezone.utc))

    def test_link_labels(self):
        got = {g.key_prefix: g.link_label for g in CATALOG}
        self.assertEqual(got["visitor.worker.requests"], "fest.link.cloudflare")
        self.assertEqual(got["visitor.uptime."], "fest.link.admin")
        self.assertEqual(got["function.parking.capture."], "fest.link.cms")
        self.assertEqual(got["capacity.cms.cpu"], "fest.link.netdata")
        self.assertEqual(got["capacity.node."], "fest.link.admin")
        self.assertEqual(got["infra.heartbeat"], "fest.link.healthchecks")
        self.assertIsNone(got["function.quake.cron"])
        self.assertIsNone(got["infra.notifier"])
        for g in CATALOG:
            self.assertEqual(g.link is None, g.link_label is None, g.key_prefix)


class BreakdownTest(unittest.TestCase):
    """内訳の表 (design「内訳」) の全項目。ts は NOW=12:00Z = 21:00 JST から数えた JST の HH:MM:SS。"""

    def bd(self, results, key):
        inp = EvalInput(NOW, CFG, results, {}, ())
        (g,) = (g for g in CATALOG if key.startswith(g.key_prefix))
        return {j.key: j.breakdown for j in g.judge(inp)}[key]

    def test_cloudflare_tables(self):
        cf = cf_result(
            worker={"by_status": [{"status": "success", "requests": 90, "errors": 0},
                                  {"status": "exceededResources", "requests": 3, "errors": 3},
                                  {"status": "scriptThrewException", "requests": 7, "errors": 7}]},
            cms={"by_status": [{"status": 200, "count": 5}, {"status": 502, "count": 40}, {"status": 404, "count": 5}]},
            media={"by_status": [{"status": 200, "count": 1}, {"status": 404, "count": 9}]})
        cache = Item("cms_cache", "c", Status.OK, {"lookups": 10, "hits": 4, "window_end": "2026-06-01T11:57:00Z",
                                                   "by_cache_status": [{"cache_status": "miss", "count": 2},
                                                                       {"cache_status": "hit", "count": 8}]})
        r = {"festival.cloudflare": result("festival.cloudflare", [*cf.items, cache])}
        for k in ("visitor.worker.error_rate", "visitor.worker.exceeded"):
            self.assertEqual(self.bd(r, k), Breakdown("fest.bd.worker_status",
                             ("fest.col.status", "fest.col.requests", "fest.col.errors"),
                             (("success", "90", "0"), ("scriptThrewException", "7", "7"),
                              ("exceededResources", "3", "3"))))
        self.assertEqual(self.bd(r, "visitor.cms.5xx_rate"), Breakdown(
            "fest.bd.http_status", ("fest.col.http_status", "fest.col.count"),
            (("502", "40"), ("200", "5"), ("404", "5"))))
        for k in ("visitor.media.4xx", "visitor.media.5xx"):
            self.assertEqual(self.bd(r, k).rows, (("404", "9"), ("200", "1")))
        self.assertEqual(self.bd(r, "capacity.cms.cache_hit"), Breakdown(
            "fest.bd.cache_status", ("fest.col.cache_status", "fest.col.count"), (("hit", "8"), ("miss", "2"))))

    def test_signage_origin_order(self):
        r = {"festival.signage": signage(7)}
        self.assertEqual(self.bd(r, "function.signage.prod"), Breakdown(
            "fest.bd.signage_origin", ("fest.col.origin", "fest.col.connections"),
            (("prod", "7"), ("dev", "0"), ("preview", "0"), ("unknown", "0"))))

    def test_quake_channels_by_name(self):
        r = {"festival.quake": quake(cron(), {"wolfx": {"test": True, "last_received_at": None},
                                              "p2p": {"receiving": False}})}
        want = Breakdown("fest.bd.quake_channel",
                         ("fest.col.channel", "fest.col.connected", "fest.col.receiving", "fest.col.last_received",
                          "fest.col.test"),
                         (("p2p", "fest.bool.true", "fest.bool.false", "20:59:55", "fest.bool.false"),
                          ("wolfx", "fest.bool.true", "fest.bool.true", "—", "fest.bool.true")))
        self.assertEqual(self.bd(r, "function.quake.channel.p2p"), want)
        self.assertEqual(self.bd(r, "function.quake.channel.wolfx"), want)

    def test_parking_cameras_by_name(self):
        r = {"festival.parking": parking(camera("2", name="南", judge_consecutive_failures=3,
                                                judge_last_succeeded_at=None),
                                         camera("1", name="北", lot="B")),
             "festival.schedule": schedule()}
        want = Breakdown("fest.bd.parking_camera",
                         ("fest.col.camera", "fest.col.lot", "fest.col.captured", "fest.col.judge_succeeded",
                          "fest.col.judge_failures"),
                         (("北", "B", "20:59:00", "20:59:00", "0"), ("南", "A", "20:59:00", "—", "3")))
        for k in ("function.parking.capture.1", "function.parking.judge.2"):
            self.assertEqual(self.bd(r, k), want)

    def test_cms_and_cloudflared_pods(self):
        cms = wl_cms()
        cms = dataclasses.replace(cms, values={**cms.values, "by_pod": [
            {"pod": "cms-b", "ready": False, "cpu_ratio": None, "throttle_ratio": 0.25, "restarts_recent": 2,
             "oom_recent": True, "crashloop": False},
            {"pod": "cms-a", "ready": True, "cpu_ratio": 0.123, "throttle_ratio": 0.0, "restarts_recent": 0,
             "oom_recent": False, "crashloop": False}]})
        cfd = Item("cloudflared", "c", Status.OK, {"pods": 2, "cpu_ratio_max": 0.1, "throttle_ratio_max": 0.0,
                                                   "by_pod": [{"pod": "cf-2", "cpu_ratio": 0.5, "throttle_ratio": None},
                                                              {"pod": "cf-1", "cpu_ratio": 0.0, "throttle_ratio": 0.1}]})
        r = {"festival.workloads": result("festival.workloads", [cms, cfd])}
        want = Breakdown("fest.bd.pod",
                         ("fest.col.pod", "fest.col.ready", "fest.col.cpu", "fest.col.throttle", "fest.col.restarts"),
                         (("cms-a", "fest.bool.true", "12.3%", "0.0%", "なし"),
                          ("cms-b", "fest.bool.false", "—", "25.0%", "OOM / 再起動 2 回")))
        for k in ("capacity.cms.ready", "capacity.cms.restarts", "capacity.cms.cpu", "capacity.cms.throttle"):
            self.assertEqual(self.bd(r, k), want)
        want = Breakdown("fest.bd.pod", ("fest.col.pod", "fest.col.cpu", "fest.col.throttle"),
                         (("cf-1", "0.0%", "10.0%"), ("cf-2", "50.0%", "—")))
        for k in ("capacity.cloudflared.cpu", "capacity.cloudflared.throttle"):
            self.assertEqual(self.bd(r, k), want)

    DB_COLS = ("fest.col.pod", "fest.col.role", "fest.col.cpu", "fest.col.memory", "fest.col.backends",
               "fest.col.lag", "fest.col.restarts")

    def test_db_joined_by_pod_primary_first(self):
        r = {"festival.workloads": result("festival.workloads", [wl_db("db-1", "replica"), wl_db("db-2"),
                                                                 wl_db("db-3", "replica", restarts_recent=1)]),
             "festival.cnpg": result("festival.cnpg", [cnpg_db("db-1", "replica", backends=4, replication_lag_s=2.5),
                                                       cnpg_db("db-2", backends=12), cnpg_db("db-3", "replica")])}
        want = Breakdown("fest.bd.db_instance", self.DB_COLS, (
            ("db-2", "primary", "10.0%", "20.0%", "12", "—", "なし"),
            ("db-1", "replica", "10.0%", "20.0%", "4", "2.5 秒", "なし"),
            ("db-3", "replica", "10.0%", "20.0%", "10", "1 秒", "再起動 1 回")))
        for k in ("capacity.db.primary_cpu", "capacity.db.connections", "capacity.db.backends.db-1",
                  "capacity.db.memory.db-1", "capacity.db.restarts", "capacity.db.unanalyzed",
                  "capacity.db.lag.db-1"):
            self.assertEqual(self.bd(r, k), want, k)

    def test_db_missing_side_is_dash(self):
        stale = Item("db.db-1", "db-1", Status.STALE, {"role": "replica"}, "x")
        r = {"festival.workloads": result("festival.workloads", [wl_db("db-2"), wl_db("db-1", "replica")]),
             "festival.cnpg": result("festival.cnpg", [cnpg_db("db-2"), stale, cnpg_db("db-9", "replica")])}
        rows = self.bd(r, "capacity.db.primary_cpu").rows
        self.assertEqual(rows, (("db-2", "primary", "10.0%", "20.0%", "10", "—", "なし"),
                                ("db-1", "replica", "10.0%", "20.0%", "—", "—", "なし"),
                                ("db-9", "replica", "—", "—", "10", "1 秒", "—")))

    def test_db_without_workloads_source(self):
        r = {"festival.cnpg": result("festival.cnpg", [cnpg_db("db-2")])}
        self.assertEqual(self.bd(r, "capacity.db.connections").rows,
                         (("db-2", "primary", "—", "—", "10", "—", "—"),))

    def test_nodes_by_name(self):
        r = {"festival.nodes": result("festival.nodes", [node("n2", ready=False, disk_ratio=None), node("n1")])}
        want = Breakdown("fest.bd.node", ("fest.col.node", "fest.col.ready", "fest.col.cpu", "fest.col.memory",
                                          "fest.col.disk"),
                         (("n1", "fest.bool.true", "10.0%", "20.0%", "30.0%"),
                          ("n2", "fest.bool.false", "10.0%", "20.0%", "—")))
        for k in ("capacity.node.n1.ready", "capacity.node.n1.cpu", "capacity.node.n2.memory", "capacity.node.n2.disk"):
            self.assertEqual(self.bd(r, k), want, k)

    def test_other_items_have_no_breakdown(self):
        cf = cf_result()
        r = {"festival.cloudflare": cf, "festival.signage": signage(1), "festival.quake": quake(cron()),
             "festival.workloads": result("festival.workloads", [wl_cms(), wl_db("db-1"),
                                                                 Item("cloudflared", "c", Status.OK, {
                                                                     "pods": 1, "cpu_ratio_max": 0, "throttle_ratio_max": 0,
                                                                     "by_pod": []})]),
             "festival.cnpg": result("festival.cnpg", [cnpg_db("db-1")]),
             "festival.nodes": result("festival.nodes", [node("n1")]),
             "connect.tunnel": result("connect.tunnel", [Item("tunnel", "t", Status.OK,
                                                              {"connections": 4, "state": "healthy"})]),
             "festival.stamp": result("festival.stamp", [Item("stamp", "s", Status.OK,
                                                              {"failed": 0, "d1_failed": 0, "rate_limited": 0})])}
        inp = EvalInput(NOW, CFG, r, {}, ())
        shown = {j.key for g in CATALOG for j in g.judge(inp) if j.breakdown is not None}
        for j in (j for g in CATALOG for j in g.judge(inp)):
            if j.key.startswith(("visitor.worker.requests", "visitor.worker.cpu_p99", "visitor.cms.origin_p95",
                                 "capacity.tunnel", "capacity.cms.rps", "function.quake.cron",
                                 "function.quake.discord", "function.stamp.", "infra.")):
                self.assertNotIn(j.key, shown)
        self.assertNotIn("capacity.tunnel", shown)
        self.assertIn("function.signage.prod", shown)


if __name__ == "__main__":
    unittest.main()
