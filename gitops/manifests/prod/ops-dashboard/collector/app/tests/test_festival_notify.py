import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import config
import store as store_mod
from festival.evaluate import Evaluation, SourceHealth
from festival.notify import MAX_CHARS, FestivalNotifier, Notice
from http_client import Http, HttpError, HttpResponse
from model import AlertState, FestivalItem, Status

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CFG = config.load(Path(__file__).resolve().parents[2] / "dashboard.toml").festival
URL = "https://discord.test/api/webhooks/123/SECRETTOKEN"


def item(key, status, **kw):
    base = dict(key=key, category="visitor", label=key.upper(), status=status, value="6.2%", threshold="異常 ≥ 5.0%",
                observed_at=None, fetched_at=NOW, source_ids=("festival.cloudflare",), link=None, note=None,
                notify=True, judged=True)
    base.update(kw)
    return FestivalItem(**base)


def ev(items=(), sources=()):
    return Evaluation(NOW, list(items), list(sources))


def alert(key, opened=NOW - timedelta(minutes=5), last=None, kind="item", open_=True):
    return AlertState(key, kind, key.upper(), open_, opened, last or opened)


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request_meta(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        r = self.responses.pop(0) if self.responses else HttpResponse(204, {}, b"")
        if isinstance(r, Exception):
            raise r
        return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def notifier(self, http=None, url=URL, cfg=CFG):
        return FestivalNotifier(cfg, self.store, http or FakeHttp(), url)


class PlanTest(Base):
    def plan(self, evaluation, alerts=None, now=NOW):
        return self.notifier().plan(evaluation, alerts or {}, now)

    def kinds(self, notices):
        return [(n.key, n.kind) for n in notices]

    def test_crit_item_opens_alert(self):
        self.assertEqual(self.kinds(self.plan(ev([item("a", Status.CRIT)]))), [("a", "alert")])

    def test_already_notified_crit_is_quiet_until_renotify_interval(self):
        a = {"a": alert("a", last=NOW - CFG.renotify + timedelta(seconds=1))}
        self.assertEqual(self.plan(ev([item("a", Status.CRIT)]), a), [])
        a = {"a": alert("a", last=NOW - CFG.renotify)}
        self.assertEqual(self.kinds(self.plan(ev([item("a", Status.CRIT)]), a)), [("a", "reminder")])

    def test_recovery_on_ok_and_on_empty(self):
        for st in (Status.OK, Status.EMPTY):
            self.assertEqual(self.kinds(self.plan(ev([item("a", st)]), {"a": alert("a")})), [("a", "recover")])

    def test_downgrade_to_warn_does_not_recover(self):
        self.assertEqual(self.plan(ev([item("a", Status.WARN)]), {"a": alert("a")}), [])

    def test_alert_on_warn_item_opens_alert_on_warn(self):
        got = self.plan(ev([item("a", Status.WARN, alert_on_warn=True)]))
        self.assertEqual(self.kinds(got), [("a", "alert")])
        self.assertTrue(got[0].warn)

    def test_alert_on_warn_item_crit_is_not_a_warn_notice(self):
        got = self.plan(ev([item("a", Status.CRIT, alert_on_warn=True)]))
        self.assertEqual((self.kinds(got), got[0].warn), ([("a", "alert")], False))

    def test_alert_on_warn_item_renotifies_while_warn(self):
        i = item("a", Status.WARN, alert_on_warn=True)
        self.assertEqual(self.plan(ev([i]), {"a": alert("a", last=NOW - CFG.renotify + timedelta(seconds=1))}), [])
        got = self.plan(ev([i]), {"a": alert("a", last=NOW - CFG.renotify)})
        self.assertEqual((self.kinds(got), got[0].warn), ([("a", "reminder")], True))

    def test_alert_on_warn_item_recovers_on_ok_and_empty(self):
        for st in (Status.OK, Status.EMPTY):
            got = self.plan(ev([item("a", st, alert_on_warn=True)]), {"a": alert("a")})
            self.assertEqual(self.kinds(got), [("a", "recover")])

    def test_alert_on_warn_item_freezes_on_stale_and_error(self):
        for st in (Status.STALE, Status.ERROR):
            self.assertEqual(self.plan(ev([item("a", st, alert_on_warn=True)]), {"a": alert("a")}), [])
            self.assertEqual(self.plan(ev([item("a", st, alert_on_warn=True)])), [])

    def test_plain_item_warn_neither_opens_nor_recovers(self):
        self.assertEqual(self.plan(ev([item("a", Status.WARN)])), [])
        self.assertEqual(self.plan(ev([item("a", Status.WARN)]), {"a": alert("a")}), [])

    def test_stale_and_error_do_not_change_state(self):
        for st in (Status.STALE, Status.ERROR):
            self.assertEqual(self.plan(ev([item("a", st)]), {"a": alert("a")}), [])
            self.assertEqual(self.plan(ev([item("a", st)])), [])

    def test_item_removed_while_open(self):
        self.assertEqual(self.kinds(self.plan(ev([]), {"a": alert("a")})), [("a", "removed")])

    def test_excluded_items_are_not_planned(self):
        items = [item("n", Status.CRIT, notify=False), item("d", Status.CRIT, judged=False, notify=True)]
        self.assertEqual(self.plan(ev(items)), [])

    def test_display_only_item_with_open_alert_is_removed_not_alerted(self):
        got = self.plan(ev([item("d", Status.CRIT, judged=False)]), {"d": alert("d")})
        self.assertEqual(self.kinds(got), [("d", "removed")])

    def test_source_failure_after_grace_is_one_notice_and_freezes_items(self):
        s = SourceHealth("festival.cloudflare", NOW - CFG.source_failure_grace, "http_429")
        got = self.plan(ev([item("a", Status.CRIT), item("b", Status.CRIT)], [s]))
        self.assertEqual(self.kinds(got), [("source:festival.cloudflare", "source_alert")])
        self.assertEqual(got[0].reason, "http_429")

    def test_source_failure_within_grace_is_quiet(self):
        s = SourceHealth("festival.cloudflare", NOW - CFG.source_failure_grace + timedelta(seconds=1), "x")
        self.assertEqual(self.plan(ev([], [s])), [])

    def test_failing_source_freezes_recovery_of_dependent_item(self):
        s = SourceHealth("festival.cloudflare", NOW - timedelta(seconds=5), "x")
        self.assertEqual(self.plan(ev([item("a", Status.OK)], [s]), {"a": alert("a")}), [])

    def test_source_recovery(self):
        a = {"source:festival.cloudflare": alert("source:festival.cloudflare", kind="source")}
        s = SourceHealth("festival.cloudflare", None, None)
        self.assertEqual(self.kinds(self.plan(ev([], [s]), a)), [("source:festival.cloudflare", "source_recover")])

    def test_source_alert_is_not_repeated_once_notified(self):
        key = "source:festival.cloudflare"
        s = SourceHealth("festival.cloudflare", NOW - timedelta(hours=1), "x")
        self.assertEqual(self.plan(ev([], [s]), {key: alert(key, kind="source")}), [])

    def test_plan_has_no_side_effects(self):
        self.plan(ev([item("a", Status.CRIT)]))
        self.assertEqual(self.store.load_alert_states(), {})


class SendTest(Base):
    def test_success_records_alert_state_and_sends_one_message_without_mentions(self):
        http = FakeHttp()
        n = self.notifier(http)
        notices = [Notice("a", "alert", "A", "6.2%", "異常 ≥ 5.0%", since=NOW),
                   Notice("b", "recover", "B", "1.0%")]
        out = n.send(notices, NOW)
        self.assertEqual(len(out.sent), 2)
        self.assertEqual(len(http.calls), 1)
        body = json.loads(http.calls[0]["data"])
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertIn("異常 1 件・回復 1 件", body["content"])
        self.assertIn("A — 現在 6.2% (異常 ≥ 5.0%)", body["content"])
        self.assertIn(CFG.page_url, body["content"])
        self.assertTrue(http.calls[0]["redact_url"])
        self.assertEqual(self.store.load_alert_states()["a"].notified_open, True)
        self.assertEqual(self.store.load_festival_state().notifier.last_success_at, NOW)

    def test_warn_notices_use_warn_line_and_header(self):
        http = FakeHttp()
        n = self.notifier(http)
        notices = [Notice("a", "alert", "A", "3.1%", "注意 ≥ 2.0%", since=NOW, warn=True),
                   Notice("b", "reminder", "B", "4.0%", "注意 ≥ 2.0%", since=NOW, warn=True),
                   Notice("c", "alert", "C", "12.4%", "異常 ≥ 10.0%", since=NOW),
                   Notice("d", "recover", "D", "1.0%")]
        n.send(notices, NOW)
        content = json.loads(http.calls[0]["data"])["content"]
        self.assertEqual(content.splitlines()[:5], [
            "[当日監視] 異常 1 件・注意 2 件・回復 1 件",
            "🟡 注意: A — 現在 3.1% (注意 ≥ 2.0%)",
            "🟡 注意 (継続): B — 現在 4.0% (注意 ≥ 2.0%)",
            "🔴 異常: C — 現在 12.4% (異常 ≥ 10.0%)",
            "🟢 回復: D — 現在 1.0%"])

    def test_header_omits_zero_groups_and_counts_warn_separately(self):
        http = FakeHttp()
        self.notifier(http).send([Notice("a", "alert", "A", "3.1%", "注意 ≥ 2.0%", since=NOW, warn=True)], NOW)
        self.assertTrue(json.loads(http.calls[0]["data"])["content"].startswith("[当日監視] 注意 1 件\n"))

    def test_recover_deletes_alert_state(self):
        self.store.apply_notifications([alert("a")], [], self.store.load_festival_state().notifier)
        self.notifier().send([Notice("a", "recover", "A")], NOW)
        self.assertEqual(self.store.load_alert_states(), {})

    def test_reminder_keeps_opened_at(self):
        opened = NOW - timedelta(hours=1)
        self.store.apply_notifications([alert("a", opened=opened)], [], self.store.load_festival_state().notifier)
        self.notifier().send([Notice("a", "reminder", "A", since=opened)], NOW)
        got = self.store.load_alert_states()["a"]
        self.assertEqual((got.opened_at, got.last_notified_at), (opened, NOW))

    def test_failure_leaves_alert_state_unchanged(self):
        for i, err in enumerate((HttpError("POST -> 500", status=500), HttpError("POST failed: URLError"))):
            n = self.notifier(FakeHttp(err))
            out = n.send([Notice("a", "alert", "A", since=NOW)], NOW + timedelta(seconds=61 * i))
            self.assertEqual(out.sent, ())
            self.assertEqual(self.store.load_alert_states(), {})
        self.assertEqual(self.store.load_festival_state().notifier.last_failure, "network")

    def test_5xx_and_network_wait_60_seconds(self):
        n = self.notifier(FakeHttp(HttpError("POST -> 503", status=503)))
        n.send([Notice("a", "alert", "A", since=NOW)], NOW)
        st = self.store.load_festival_state().notifier
        self.assertEqual((st.last_failure, st.not_before), ("http_503", NOW + timedelta(seconds=60)))
        http = FakeHttp()
        n.http = http
        out = n.send([Notice("a", "alert", "A", since=NOW)], NOW + timedelta(seconds=59))
        self.assertEqual((http.calls, out.deferred[0].key), ([], "a"))
        n.send([Notice("a", "alert", "A", since=NOW)], NOW + timedelta(seconds=60))
        self.assertEqual(len(http.calls), 1)

    def test_429_waits_until_retry_after(self):
        n = self.notifier(FakeHttp(HttpError("POST -> 429", status=429, retry_after=120)))
        n.send([Notice("a", "alert", "A", since=NOW)], NOW)
        st = self.store.load_festival_state().notifier
        self.assertEqual((st.last_failure, st.not_before), ("http_429", NOW + timedelta(seconds=120)))

    def test_remaining_zero_waits_until_reset(self):
        h = HttpResponse(204, {"x-ratelimit-remaining": "0", "x-ratelimit-reset-after": "3.5"}, b"")
        n = self.notifier(FakeHttp(h))
        out = n.send([Notice("a", "alert", "A", since=NOW)], NOW)
        self.assertEqual(len(out.sent), 1)
        self.assertEqual(self.store.load_festival_state().notifier.not_before, NOW + timedelta(seconds=3))  # 秒精度

    def test_401_403_404_halt_until_restart(self):
        for code in (401, 403, 404):
            http = FakeHttp(HttpError(f"POST -> {code}", status=code))
            n = self.notifier(http)
            n.send([Notice("a", "alert", "A", since=NOW)], NOW)
            st = self.store.load_festival_state().notifier
            self.assertEqual((st.halted, st.last_failure), (True, f"disabled_http_{code}"))
            n.send([Notice("a", "alert", "A", since=NOW)], NOW + timedelta(hours=1))
            self.assertEqual(len(http.calls), 1)  # 停止中は再送しない
            self.notifier()  # 再起動 (停止を解除する)
            self.assertFalse(self.store.load_festival_state().notifier.halted)

    def test_unconfigured_never_sends(self):
        http = FakeHttp()
        n = self.notifier(http, url=None)
        out = n.send([Notice("a", "alert", "A", since=NOW)], NOW)
        self.assertEqual((http.calls, len(out.deferred), n.configured), ([], 1, False))

    def test_long_batch_is_truncated_to_2000_chars_and_rest_deferred(self):
        http = FakeHttp()
        notices = [Notice(f"k{i}", "alert", "L" * 100, "v", "t", since=NOW) for i in range(40)]
        out = self.notifier(http).send(notices, NOW)
        content = json.loads(http.calls[0]["data"])["content"]
        self.assertLessEqual(len(content), MAX_CHARS)
        self.assertTrue(out.sent and out.deferred)
        self.assertEqual(len(out.sent) + len(out.deferred), 40)
        self.assertEqual(set(self.store.load_alert_states()), {n.key for n in out.sent})
        self.assertIn(f"異常 {len(out.sent)} 件", content)


class RedactionTest(Base):
    def test_error_text_never_contains_url_with_real_http(self):
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(429)
                self.send_header("Retry-After", "7")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_port}/api/webhooks/1/SECRETTOKEN"
            n = self.notifier(Http(timeout=3), url=url)
            out = n.send([Notice("a", "alert", "A", since=NOW)], NOW)
        finally:
            srv.shutdown()
        st = self.store.load_festival_state().notifier
        self.assertEqual((out.failure, st.not_before), ("http_429", NOW + timedelta(seconds=7)))
        self.assertNotIn("SECRETTOKEN", repr(out) + str(st))

    def test_connection_failure_message_has_no_url(self):
        url = "http://127.0.0.1:9/api/webhooks/1/SECRETTOKEN"
        with self.assertRaises(HttpError) as cm:
            Http(timeout=2).request_meta("POST", url, data=b"{}", redact_url=True)
        self.assertNotIn("SECRETTOKEN", str(cm.exception))
        n = self.notifier(Http(timeout=2), url=url)
        out = n.send([Notice("a", "alert", "A", since=NOW)], NOW)
        self.assertEqual(out.failure, "network")


if __name__ == "__main__":
    unittest.main()
