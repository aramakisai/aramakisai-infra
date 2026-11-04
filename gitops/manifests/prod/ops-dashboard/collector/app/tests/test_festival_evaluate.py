import dataclasses
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import model
import store as store_mod
from festival.evaluate import FestivalEvaluator
from model import AlertState, Item, SourceResult, Status

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
