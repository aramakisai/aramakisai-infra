import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import store as store_mod
from model import AlertState, FestivalItem, NotifierState, Status

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def item(key, status=Status.OK, **kw):
    base = dict(key=key, category="visitor", label=key.upper(), status=status, value="1.0%", threshold="注意 ≥ 1.0%",
                observed_at=NOW - timedelta(minutes=3), fetched_at=NOW, source_ids=("festival.cloudflare", "ci.github"),
                link="https://example.test/x", note=None, notify=True, judged=True)
    base.update(kw)
    return FestivalItem(**base)


class FestivalStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_tables_exist_after_startup(self):
        names = {r[0] for r in self.store.query("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"festival_items", "festival_alerts", "festival_state"} <= names)

    def test_items_roundtrip_including_display_only_and_nulls(self):
        a = item("visitor.a", Status.CRIT, note="n")
        b = item("visitor.b", observed_at=None, fetched_at=None, threshold=None, link=None, notify=False,
                 judged=False, source_ids=())
        self.store.replace_festival_items([a, b], NOW)
        self.assertEqual(self.store.load_festival_items(), [a, b])

    def test_replace_drops_items_not_in_new_evaluation_and_sets_evaluated_at(self):
        self.store.replace_festival_items([item("a"), item("b")], NOW)
        later = NOW + timedelta(seconds=15)
        self.store.replace_festival_items([item("b", Status.WARN)], later)
        self.assertEqual([(i.key, i.status) for i in self.store.load_festival_items()], [("b", Status.WARN)])
        self.assertEqual(self.store.load_festival_state().evaluated_at, later)

    def test_replace_is_one_transaction(self):
        self.store.replace_festival_items([item("a")], NOW)
        bad = item("b", category=None)  # NOT NULL 違反で保存時に失敗させる
        with self.assertRaises(Exception):
            self.store.replace_festival_items([item("c"), bad], NOW + timedelta(seconds=15))
        self.assertEqual([i.key for i in self.store.load_festival_items()], ["a"])
        self.assertEqual(self.store.load_festival_state().evaluated_at, NOW)

    def test_state_defaults_before_any_write(self):
        st = self.store.load_festival_state()
        self.assertIsNone(st.evaluated_at)
        self.assertIsNone(st.last_eval_error)
        self.assertEqual(st.notifier, NotifierState(None, None, None, None, False))
        self.assertIsNone(st.heartbeat_ok_at)

    def test_eval_error_recorded_without_moving_evaluated_at_and_cleared_by_next_success(self):
        self.store.replace_festival_items([item("a")], NOW)
        self.store.save_festival_eval_error("ValueError: boom")
        st = self.store.load_festival_state()
        self.assertEqual((st.evaluated_at, st.last_eval_error), (NOW, "ValueError: boom"))
        self.store.replace_festival_items([item("a")], NOW + timedelta(seconds=15))
        self.assertIsNone(self.store.load_festival_state().last_eval_error)

    def test_eval_error_before_any_evaluation(self):
        self.store.save_festival_eval_error("x")
        st = self.store.load_festival_state()
        self.assertEqual((st.evaluated_at, st.last_eval_error), (None, "x"))

    def test_heartbeat_success_and_failure_are_kept_separately(self):
        self.store.save_festival_heartbeat(NOW, True, None)
        self.store.save_festival_heartbeat(NOW + timedelta(minutes=1), False, "http_500")
        st = self.store.load_festival_state()
        self.assertEqual((st.heartbeat_ok_at, st.heartbeat_failed_at, st.heartbeat_failure),
                         (NOW, NOW + timedelta(minutes=1), "http_500"))
        self.store.save_festival_heartbeat(NOW + timedelta(minutes=2), True, None)
        st = self.store.load_festival_state()
        self.assertEqual(st.heartbeat_ok_at, NOW + timedelta(minutes=2))
        self.assertEqual(st.heartbeat_failure, "http_500")

    def test_heartbeat_does_not_touch_other_state(self):
        self.store.replace_festival_items([item("a")], NOW)
        self.store.save_festival_heartbeat(NOW, True, None)
        self.assertEqual(self.store.load_festival_state().evaluated_at, NOW)

    def test_alert_states_apply_upsert_delete_and_notifier_in_one_write(self):
        a = AlertState("visitor.a", "item", "A", True, NOW, NOW)
        s = AlertState("source:festival.cloudflare", "source", "Cloudflare", False, NOW, None)
        n1 = NotifierState(NOW, None, None, None, False)
        self.store.apply_notifications([a, s], [], n1)
        self.assertEqual(self.store.load_alert_states(), {a.key: a, s.key: s})
        self.assertEqual(self.store.load_festival_state().notifier, n1)

        a2 = AlertState("visitor.a", "item", "A", True, NOW, NOW + timedelta(minutes=30))
        n2 = NotifierState(NOW, NOW + timedelta(minutes=31), "http_429", NOW + timedelta(minutes=32), False)
        self.store.apply_notifications([a2], [s.key], n2)
        self.assertEqual(self.store.load_alert_states(), {a.key: a2})
        self.assertEqual(self.store.load_festival_state().notifier, n2)

    def test_notifier_halted_roundtrip(self):
        n = NotifierState(None, NOW, "disabled_http_404", None, True)
        self.store.apply_notifications([], [], n)
        self.assertEqual(self.store.load_festival_state().notifier, n)

    def test_apply_notifications_rolls_back_everything_on_failure(self):
        a = AlertState("k", "item", "A", True, NOW, None)
        self.store.apply_notifications([a], [], NotifierState(NOW, None, None, None, False))
        bad = AlertState("k2", None, "B", True, NOW, None)  # kind は NOT NULL
        with self.assertRaises(Exception):
            self.store.apply_notifications([bad], ["k"], NotifierState(None, NOW, "x", None, True))
        self.assertEqual(set(self.store.load_alert_states()), {"k"})
        self.assertEqual(self.store.load_festival_state().notifier, NotifierState(NOW, None, None, None, False))

    def test_notifier_update_keeps_eval_and_heartbeat_columns(self):
        self.store.replace_festival_items([item("a")], NOW)
        self.store.save_festival_heartbeat(NOW, True, None)
        self.store.apply_notifications([], [], NotifierState(NOW, None, None, None, False))
        st = self.store.load_festival_state()
        self.assertEqual((st.evaluated_at, st.heartbeat_ok_at), (NOW, NOW))

    def test_purge_leaves_festival_tables_alone(self):
        old = NOW - timedelta(days=1000)
        self.store.replace_festival_items([item("a", observed_at=old, fetched_at=old)], old)
        self.store.apply_notifications([AlertState("k", "item", "A", True, old, old)], [], NotifierState(old, None, None, None, False))
        self.store.purge(NOW)
        self.assertEqual(len(self.store.load_festival_items()), 1)
        self.assertEqual(len(self.store.load_alert_states()), 1)
        self.assertEqual(self.store.load_festival_state().notifier.last_success_at, old)


if __name__ == "__main__":
    unittest.main()
