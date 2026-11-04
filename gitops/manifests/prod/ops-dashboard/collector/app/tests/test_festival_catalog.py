import dataclasses
import unittest
from datetime import datetime, timedelta, timezone
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
    def run_(self, cams=None, sched=None, now=None):
        res = {"festival.parking": cams or parking(), "festival.schedule": sched or schedule()}
        inp = EvalInput(now or NOW, CFG, res, {}, ())
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

    def test_judge_worse_of_failures_and_age(self):
        j = lambda **kw: self.run_(parking(camera(**kw)))["function.parking.judge.1"].status
        self.assertEqual(j(judge_consecutive_failures=1), Status.WARN)
        self.assertEqual(j(judge_consecutive_failures=5), Status.CRIT)
        self.assertEqual(j(judge_last_succeeded_at=ago(minutes=10)), Status.WARN)
        self.assertEqual(j(judge_last_succeeded_at=ago(minutes=20), judge_consecutive_failures=1), Status.CRIT)
        self.assertEqual(j(judge_last_succeeded_at=None), Status.CRIT)

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
    def status(self, prod, installed):
        cfg = dataclasses.replace(CFG, signage=dataclasses.replace(CFG.signage, installed=installed))
        return fjudge({"festival.signage": signage(prod)}, cfg)["function.signage.prod"]

    def test_against_installed(self):
        s = lambda prod: self.status(prod, 10).status
        self.assertEqual([s(10), s(12), s(9), s(8), s(7), s(0)],
                         [Status.OK, Status.OK, Status.WARN, Status.WARN, Status.CRIT, Status.CRIT])
        self.assertEqual(self.status(9, 10).value, "9 / 10 台")

    def test_zero_installed_is_display_only(self):
        j = self.status(0, 0)
        self.assertEqual((j.status, j.judged), (Status.OK, False))


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
        self.assertEqual({k: j.status for k, j in js.items()}, {
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
         "cpu_ratio_max": 0.1, "throttle_ratio_max": 0.0, **over}
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
                                                            {"state": "healthy", "connections": 4})])})
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

    def test_restart_rules_cms_and_db(self):
        w = lambda **o: self.j({"festival.workloads": result("festival.workloads", [wl_cms(**o), wl_db("db-1", **o)])})
        self.assertEqual(w(restarts_recent=1)["capacity.cms.restarts"].status, Status.WARN)
        self.assertEqual(w(oom_recent=True)["capacity.cms.restarts"].status, Status.CRIT)
        self.assertEqual(w(crashloop=True)["capacity.db.restarts"].status, Status.CRIT)
        self.assertEqual(w(restarts_recent=2)["capacity.db.restarts"].value, "db-1 (再起動 2 回)")
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
                         cnpg_db("p", backends=70, unanalyzed_tables=10), cnpg_db("r", "replica", replication_lag_s=10)])})
        self.assertEqual(js["capacity.db.primary_cpu"].status, Status.CRIT)  # replica の 0.99 は混ざらない
        self.assertEqual(js["capacity.db.connections"].status, Status.WARN)  # 0.7
        self.assertEqual(js["capacity.db.unanalyzed"].status, Status.CRIT)
        self.assertEqual(js["capacity.db.lag.r"].status, Status.WARN)
        self.assertNotIn("capacity.db.lag.p", js)

    def test_rps_excludes_media(self):
        cf = lambda cms, media: self.j({"festival.cloudflare": result("festival.cloudflare", [
            Item("cms", "cms", Status.OK, {"requests": cms, "window_end": "2026-06-01T11:57:00Z"}),
            Item("media", "media", Status.OK, {"requests": media})])})["capacity.cms.rps"]
        self.assertEqual(cf(30000, 6000).status, Status.WARN)  # 80 req/s ちょうど
        self.assertEqual(cf(30000, 30000).status, Status.OK)  # 全部メディアなら 0

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
            node("a", ready=False, cpu_ratio=0.85, memory_ratio=0.95, disk_ratio=None)])})
        self.assertEqual(js["capacity.node.a.ready"].status, Status.CRIT)
        self.assertEqual(js["capacity.node.a.cpu"].status, Status.WARN)
        self.assertEqual(js["capacity.node.a.memory"].status, Status.CRIT)
        self.assertEqual((js["capacity.node.a.disk"].status, js["capacity.node.a.disk"].value), (Status.OK, "—"))

    def test_tunnel_takes_worse_of_state_and_connections(self):
        t = lambda status, conns: self.j({"connect.tunnel": result("connect.tunnel", [
            Item("tunnel", "tunnel", status, {"state": "x", "connections": conns})])})["capacity.tunnel"].status
        self.assertEqual(t(Status.OK, 4), Status.OK)
        self.assertEqual(t(Status.OK, 3), Status.WARN)
        self.assertEqual(t(Status.OK, 0), Status.CRIT)
        self.assertEqual(t(Status.CRIT, 4), Status.CRIT)


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


if __name__ == "__main__":
    unittest.main()
