import dataclasses
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import model
import store as store_mod
from unittest import mock

import festival.evaluate as evaluate_mod
from festival.catalog import Category, ItemGroup, Judgement
from festival.evaluate import FestivalEvaluator
from model import AlertState, Breakdown, Item, SourceResult, Status
from render.labels import label

PROD_TOML = Path(__file__).resolve().parents[2] / "dashboard.toml"
NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CFG = config.load(PROD_TOML).festival

WORKER = Item("worker", "worker", Status.OK, {"requests": 1000, "errors": 80, "exceeded": 0, "cpu_p99_ms": 10,
                                               "window_end": "2026-06-01T11:57:00Z"})


def cf(status=Status.OK, fetched=NOW, error=None):
    return SourceResult("festival.cloudflare", status, fetched, fetched, (WORKER,), error)


class Recorder:
    configured = True

    def __init__(self):
        self.beats, self.plans = [], []

    def beat(self, now):
        self.beats.append(now)

    def plan(self, ev, alerts, now):
        self.plans.append(ev)
        return []


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))
        self.results = {}
        self.now = NOW
        self.rec = Recorder()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ev(self, cfg=CFG, latest=None):
        return FestivalEvaluator(cfg, latest or (lambda: dict(self.results)), self.store, self.rec,
                                 lambda: self.now, heartbeat=self.rec, internal_networks=lambda: ())

    def items(self, ev):
        return {i.key: i for i in ev.evaluate_once().items}

    def test_judged_value_passes_through_when_source_is_fresh(self):
        self.results["festival.cloudflare"] = cf()
        i = self.items(self.ev())["visitor.worker.error_rate"]
        self.assertEqual(i.status, Status.CRIT)  # 8.0% は異常
        self.assertTrue(i.notify and i.judged)

    def test_source_error_overrides_status_and_keeps_last_value(self):
        self.results["festival.cloudflare"] = cf(Status.ERROR, error="HttpError: boom")
        i = self.items(self.ev())["visitor.worker.error_rate"]
        self.assertEqual(i.status, Status.ERROR)
        self.assertIn("8.0%", i.value)
        self.assertEqual(i.note, "HttpError: boom")

    def test_stale_fetch_beyond_max_age_overrides(self):
        self.results["festival.cloudflare"] = cf(fetched=NOW - CFG.max_age["festival.cloudflare"] - timedelta(seconds=1))
        self.assertEqual(self.items(self.ev())["visitor.worker.error_rate"].status, Status.STALE)

    def test_fetch_exactly_at_max_age_is_fresh(self):
        self.results["festival.cloudflare"] = cf(fetched=NOW - CFG.max_age["festival.cloudflare"])
        self.assertEqual(self.items(self.ev())["visitor.worker.error_rate"].status, Status.CRIT)

    def test_override_applies_to_display_only_items(self):
        th = {k: v for k, v in CFG.thresholds.items() if k != "cms_cache_hit_ratio"}
        cfg = dataclasses.replace(CFG, thresholds=th)
        cache = Item("cms_cache", "cms_cache", Status.OK, {"lookups": 100, "hits": 50, "window_end": "2026-06-01T11:57:00Z"})
        self.results["festival.cloudflare"] = SourceResult("festival.cloudflare", Status.ERROR, NOW, NOW, (WORKER, cache), "x")
        i = self.items(self.ev(cfg))["capacity.cms.cache_hit"]
        self.assertFalse(i.judged)
        self.assertEqual(i.status, Status.ERROR)

    def test_never_fetched_source_yields_error_item(self):
        items = self.items(self.ev())
        placeholders = [i for i in items.values() if i.status is Status.ERROR and i.note == "未取得"]
        self.assertTrue(placeholders)
        self.assertTrue(any("festival.cloudflare" in i.source_ids for i in placeholders))

    def test_item_note_means_stale(self):
        self.results["festival.cloudflare"] = SourceResult("festival.cloudflare", Status.OK, NOW, NOW, (
            WORKER, Item("cms_cache", "cms_cache", Status.OK, {"lookups": 100, "hits": 50, "window_end": "2026-06-01T11:57:00Z"}, note="取得遅延")))
        # note 付きの項目は取得遅延
        items = self.items(self.ev())
        notes = [i for i in items.values() if i.note == "取得遅延"]
        self.assertTrue(all(i.status is Status.STALE for i in notes))

    def test_stale_cnpg_pod_does_not_stop_evaluation(self):
        from helpers import FakeHttp, make_ctx
        from http_client import HttpError
        from sources import festival_cnpg, festival_workloads as fw
        from test_festival_source_cnpg import PRIMARY, REPLICA, ep
        ok = {"primary": PRIMARY.encode(), "replica": REPLICA.encode()}
        for down in ("primary", "replica"):
            with self.subTest(stale=down):
                fw._db_endpoints = (ep("p", "10.1.0.1", "primary"), ep("r", "10.1.0.2", "replica"))
                routes = {f"http://10.1.0.{n}:9187/metrics": HttpError("GET -> 500", status=500) if role == down else ok[role]
                          for n, role in ((1, "primary"), (2, "replica"))}
                cnpg = festival_cnpg.SOURCES[0].fetch(make_ctx(http=FakeHttp(routes)))
                self.results = {"festival.cloudflare": cf(),
                                "festival.cnpg": SourceResult("festival.cnpg", Status.OK, NOW, NOW, cnpg.items, None)}
                db = {k: i for k, i in self.items(self.ev()).items() if k.startswith("capacity.db.")}
                self.assertTrue(any(i.status is Status.STALE and i.note in evaluate_mod.STALE_NOTES for i in db.values()))
                self.assertTrue(any(i.status is Status.OK for i in db.values()))
        fw._db_endpoints = ()

    def _iso(self, **kw):
        return model.iso(NOW - timedelta(**kw))

    def test_display_note_keeps_crit_for_quake_discord(self):
        q = SourceResult("festival.quake", Status.OK, NOW, NOW, (
            Item("cron", "cron", Status.OK, {"last_run_at": self._iso(minutes=1), "in_period": True}),
            Item("unreachable", "u", Status.OK, {"value": False}),
            Item("discord", "d", Status.OK, {"configured": True, "disabled": True, "last_success_at": self._iso(hours=1),
                                             "last_failure_at": self._iso(minutes=1), "last_failure": "http_404"})), None)
        self.results["festival.quake"] = q
        i = self.items(self.ev())["function.quake.discord"]
        self.assertEqual((i.status, i.note), (Status.CRIT, "http_404"))

    def test_display_note_keeps_crit_for_parking_judge_failures(self):
        cam = Item("camera.1", "北", Status.OK, {
            "name": "北", "lot": "A", "last_captured_at": self._iso(minutes=1), "device_received_at": None,
            "judge_last_succeeded_at": self._iso(hours=3), "judge_last_failed_at": self._iso(minutes=1),
            "judge_last_error": "boom", "judge_consecutive_failures": 50, "lot_status_updated_at": None})
        sched = Item("monitor", "m", Status.OK, {"start": "00:00", "end": "23:59"})
        day = Item("event_day.0", "d0", Status.OK, {"start_at": "2026-05-31T15:00:00Z", "end_at": "2026-06-01T15:00:00Z"})
        self.results["festival.parking"] = SourceResult("festival.parking", Status.OK, NOW, NOW, (cam,), None)
        self.results["festival.schedule"] = SourceResult("festival.schedule", Status.OK, NOW, NOW, (sched, day), None)
        cfg = dataclasses.replace(CFG, parking=dataclasses.replace(CFG.parking, enabled=True))
        i = self.items(self.ev(cfg))["function.parking.judge.1"]
        self.assertEqual((i.status, i.note), (Status.CRIT, "boom"))

    def test_source_stale_notes_mean_stale(self):
        for note in ("取得遅延", label("note.metrics_missing")):
            self.results["festival.workloads"] = SourceResult("festival.workloads", Status.OK, NOW, NOW, (
                Item("cms", "cms", Status.OK, {"replicas": 2, "ready": 2, "restarts_recent": 0, "oom_recent": False,
                                           "crashloop": False, "cpu_ratio_max": 0.1, "throttle_ratio_max": None}, note=note),), None)
            i = self.items(self.ev())["capacity.cms.throttle"]
            self.assertEqual((i.status, i.note), (Status.STALE, note))

    def test_disabled_sources_produce_no_items(self):
        self.results["festival.cloudflare"] = cf()
        cfg = dataclasses.replace(CFG, disabled_sources=["festival.cloudflare", "festival.stamp"])
        keys = self.items(self.ev(cfg)).keys()
        self.assertFalse([k for k in keys if k.startswith(("visitor.worker", "function.stamp"))])

    def test_parking_disabled_produces_no_parking_items(self):
        self.assertFalse(CFG.parking.enabled)
        self.assertFalse([k for k in self.items(self.ev()) if k.startswith("function.parking")])
        cfg = dataclasses.replace(CFG, parking=dataclasses.replace(CFG.parking, enabled=True))
        # 有効にすれば、結果が無い間は取得失敗の項目として現れる
        self.assertTrue([k for k in self.items(self.ev(cfg)) if k.startswith("function.parking")])

    def test_items_are_persisted_and_replace_previous(self):
        self.results["festival.cloudflare"] = cf()
        e = self.ev()
        e.evaluate_once()
        self.assertIn("visitor.worker.error_rate", {i.key for i in self.store.load_festival_items()})
        self.assertEqual(self.store.load_festival_state().evaluated_at, NOW)

    def test_failing_since_is_stable_and_clears_on_recovery(self):
        self.results["festival.cloudflare"] = cf(Status.ERROR, error="x")
        e = self.ev()
        since = lambda ev: {s.source_id: s.failing_since for s in ev.sources}["festival.cloudflare"]  # noqa: E731
        self.assertEqual(since(e.evaluate_once()), NOW)
        self.now = NOW + timedelta(minutes=2)
        self.assertEqual(since(e.evaluate_once()), NOW)
        self.results["festival.cloudflare"] = cf(fetched=self.now)
        self.assertIsNone(since(e.evaluate_once()))

    def test_failing_since_restored_from_alert_state_after_restart(self):
        opened = NOW - timedelta(minutes=10)
        self.store.apply_notifications([AlertState("source:festival.cloudflare", "source", "s", True, opened, opened)],
                                       [], self.store.load_festival_state().notifier)
        self.results["festival.cloudflare"] = cf(Status.ERROR, error="x")
        ev = self.ev().evaluate_once()
        self.assertEqual({s.source_id: s.failing_since for s in ev.sources}["festival.cloudflare"], opened)

    def test_exception_is_recorded_and_next_tick_continues(self):
        calls = []

        def latest():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return {}

        e = self.ev(latest=latest)
        e.tick()
        st = self.store.load_festival_state()
        self.assertIn("boom", st.last_eval_error)
        self.assertIsNone(st.evaluated_at)
        self.assertEqual(self.rec.beats, [])  # 例外の評価では ping しない
        e.tick()
        st = self.store.load_festival_state()
        self.assertIsNone(st.last_eval_error)
        self.assertEqual(st.evaluated_at, NOW)
        self.assertEqual(self.rec.beats, [NOW])

    def test_falco_counts_use_window(self):
        self.store.insert_falco_event({"rule": "r", "priority": "Critical"}, NOW - timedelta(minutes=1))
        self.store.insert_falco_event({"rule": "r", "priority": "Critical"}, NOW - CFG.falco.window - timedelta(minutes=1))
        i = self.items(self.ev())["infra.falco"]
        self.assertEqual(i.status, Status.CRIT)
        self.assertIn("1 件", i.value)


if __name__ == "__main__":
    unittest.main()


BD = Breakdown("fest.bd.x", ("fest.col.a",), (("1",),))
SRC = "festival.cloudflare"


def judgement(key, **kw):
    base = dict(key=key, label=key, status=Status.OK, value="v", threshold=None, observed_at=NOW,
                metric=1.0, unit="count", warn=2.0, crit=3.0, breakdown=BD)
    return Judgement(**{**base, **kw})


class HistoryTest(unittest.TestCase):
    """CATALOG を差し替え、評価器が Judgement を FestivalItem に移し 1 分値を記録する経路だけを確かめる。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))
        self.now = NOW
        self.fetched = NOW
        self.judgements = []
        group = ItemGroup("t.", Category.VISITOR, (SRC,), "https://x", "fest.link.x", True, lambda inp: self.judgements)
        p = mock.patch.object(evaluate_mod, "CATALOG", (group,))
        p.start()
        self.addCleanup(p.stop)
        self.e = FestivalEvaluator(CFG, self.latest, self.store, None, lambda: self.now, internal_networks=lambda: ())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def latest(self):
        return {SRC: cf(fetched=self.fetched)}

    def hist(self):
        return {k: v for k, v in self.store.load_festival_history(NOW - timedelta(days=3), NOW + timedelta(days=3)).items()}

    def test_judgement_fields_move_to_item(self):
        self.judgements = [judgement("t.a")]
        i = self.e.evaluate_once().items[0]
        self.assertEqual((i.metric, i.unit, i.warn, i.crit, i.breakdown, i.link_label), (1.0, "count", 2.0, 3.0, BD, "fest.link.x"))
        saved = self.store.load_festival_items()[0]
        self.assertEqual((saved.metric, saved.breakdown, saved.link_label), (1.0, BD, "fest.link.x"))

    def test_stale_and_error_items_keep_breakdown(self):
        self.judgements = [judgement("t.a")]
        self.fetched = NOW - CFG.max_age[SRC] - timedelta(seconds=1)
        i = self.e.evaluate_once().items[0]
        self.assertEqual((i.status, i.breakdown, i.metric), (Status.STALE, BD, 1.0))
        self.fetched = NOW
        self.latest = lambda: {SRC: cf(Status.ERROR, error="x")}
        self.e.latest = self.latest
        i = self.e.evaluate_once().items[0]
        self.assertEqual((i.status, i.breakdown), (Status.ERROR, BD))

    def test_records_only_on_first_evaluation_of_a_new_minute(self):
        self.judgements = [judgement("t.a", metric=1.0)]
        self.e.evaluate_once()
        self.judgements = [judgement("t.a", metric=9.0)]
        self.now = NOW + timedelta(seconds=15)
        self.e.evaluate_once()
        self.assertEqual(self.hist()["t.a"], [(NOW, 1.0)])
        self.now = NOW + timedelta(seconds=75)
        self.e.evaluate_once()
        self.assertEqual(self.hist()["t.a"], [(NOW, 1.0), (NOW + timedelta(minutes=1), 9.0)])

    def test_minute_is_truncated(self):
        self.judgements = [judgement("t.a")]
        self.now = NOW + timedelta(seconds=40)
        self.e.evaluate_once()
        self.assertEqual(self.hist()["t.a"][0][0], NOW)

    def test_excluded_items_are_not_recorded(self):
        self.judgements = [
            judgement("t.ok"),
            judgement("t.empty", status=Status.EMPTY),
            judgement("t.nometric", metric=None),
            judgement("t.nounit", unit=None),
            judgement("t.stale", note="取得遅延"),
            judgement("t.display", judged=False, warn=None, crit=None),
        ]
        self.e.evaluate_once()
        self.assertEqual(set(self.hist()), {"t.ok", "t.display"})

    def test_error_and_stale_sources_are_not_recorded(self):
        self.judgements = [judgement("t.a")]
        self.fetched = NOW - CFG.max_age[SRC] - timedelta(seconds=1)
        self.e.evaluate_once()
        self.assertEqual(self.hist(), {})

    def test_status_is_not_recorded_as_value(self):
        self.judgements = [judgement("t.a", status=Status.CRIT, metric=0.0)]
        self.e.evaluate_once()
        self.assertEqual(self.hist()["t.a"], [(NOW, 0.0)])

    def test_unrecorded_minute_is_not_filled(self):
        self.judgements = [judgement("t.a")]
        self.e.evaluate_once()
        self.now = NOW + timedelta(minutes=1)
        self.judgements = [judgement("t.a", metric=None)]
        self.e.evaluate_once()
        self.now = NOW + timedelta(minutes=2)
        self.judgements = [judgement("t.a", metric=3.0)]
        self.e.evaluate_once()
        self.assertEqual([m for m, _ in self.hist()["t.a"]], [NOW, NOW + timedelta(minutes=2)])

    def test_rows_older_than_24h_are_deleted(self):
        self.judgements = [judgement("t.a")]
        self.e.evaluate_once()
        self.now = NOW + timedelta(hours=24, minutes=1)
        self.fetched = self.now
        self.e.evaluate_once()
        self.assertEqual([m for m, _ in self.hist()["t.a"]], [self.now.replace(second=0)])

    def test_restart_overwrites_same_minute(self):
        self.judgements = [judgement("t.a", metric=1.0)]
        self.e.evaluate_once()
        self.judgements = [judgement("t.a", metric=5.0)]
        FestivalEvaluator(CFG, self.latest, self.store, None, lambda: self.now,
                          internal_networks=lambda: ()).evaluate_once()
        self.assertEqual(self.hist()["t.a"], [(NOW, 5.0)])

    def test_failed_write_does_not_consume_the_minute(self):
        self.judgements = [judgement("t.a")]
        with mock.patch.object(self.store, "replace_festival_items", side_effect=RuntimeError("boom")):
            self.e.tick()
        self.assertEqual(self.store.load_festival_state().last_eval_error, "RuntimeError: boom")
        self.e.tick()
        self.assertEqual(self.hist()["t.a"], [(NOW, 1.0)])
