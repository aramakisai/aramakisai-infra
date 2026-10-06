import json
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
import threading

import falco_ingest
from helpers import FakeHttp, make_ctx, temp_store
from model import Status
from sources import auth_zitadel, security_falco

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
URL = "http://zitadel.zitadel.svc.cluster.local:8080/admin/v1/events/_search"


def ev(seq, typ, when=None, user="u1"):
    when = when or f"2026-06-01T10:00:{seq:02d}Z"
    return {"sequence": str(seq), "creationDate": when, "type": {"type": typ}, "aggregate": {"id": user}}


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.d, self.st = temp_store()

    def tearDown(self):
        self.st.close()
        self.d.cleanup()

    def run_fetch(self, events):
        http = FakeHttp({URL: lambda **kw: {"events": sorted((e for e in events if e["creationDate"] >= kw["body"].get("from", "")),
                                                key=lambda e: e["creationDate"])}})
        ctx = make_ctx(http=http, store=self.st, env={"OPS_ZITADEL_READER_PAT": "p"})
        return auth_zitadel.SOURCES[0].fetch(ctx), http

    def test_counts_and_cursor(self):
        events = [ev(1, "user.human.password.check.failed"), ev(2, "user.human.password.check.failed"),
                  ev(3, "user.locked"), ev(4, "user.human.mfa.otp.check.failed", "2026-06-01T11:30:00Z")]
        r, http = self.run_fetch(events)
        by = {i.key: i for i in r.items}
        # 10:00Z = JST 19 時
        self.assertEqual(by["auth.hour.2026-06-01T19"].values["password"], 2)
        self.assertEqual(by["auth.hour.2026-06-01T19"].values["locked"], 1)
        self.assertEqual(by["auth.hour.2026-06-01T20"].values["otp"], 1)
        self.assertEqual(by["auth.day.2026-06-01"].values["password"], 2)
        self.assertEqual(self.st.get_cursor("auth_cursor"), "2026-06-01T11:30:00Z")
        self.assertEqual(http.calls[0]["bearer"], "p")
        self.assertNotIn("from", http.calls[0]["body"])

        # 2 回目はカーソル以降だけを要求し、重複を作らない
        r2, http2 = self.run_fetch(events + [ev(5, "user.human.password.check.failed", "2026-06-01T11:40:00Z")])
        self.assertEqual(http2.calls[0]["body"]["from"], "2026-06-01T11:30:00Z")
        self.assertEqual({i.key: i for i in r2.items}["auth.day.2026-06-01"].values["password"], 3)
        self.assertEqual(self.st.query("SELECT count(*) FROM auth_events")[0][0], 5)

    def test_same_sequence_in_different_aggregates_is_kept(self):
        events = [ev(1, "user.locked", "2026-06-01T10:00:00Z", user="a"),
                  ev(1, "user.human.password.check.failed", "2026-06-01T10:00:01Z", user="b")]
        self.run_fetch(events)
        self.assertEqual(self.st.query("SELECT count(*) FROM auth_events")[0][0], 2)

    def test_lower_sequence_after_cursor_is_fetched(self):
        # 集約ごとの採番なので、後から起きたイベントの sequence が既存カーソルより小さいことがある
        self.run_fetch([ev(9, "user.locked", "2026-06-01T10:00:00Z", user="a")])
        self.run_fetch([ev(9, "user.locked", "2026-06-01T10:00:00Z", user="a"),
                        ev(2, "user.human.password.check.failed", "2026-06-01T10:05:00Z", user="b")])
        self.assertEqual(self.st.query("SELECT count(*) FROM auth_events")[0][0], 2)

    def test_recent_order(self):
        r, _ = self.run_fetch([ev(1, "user.locked"), ev(2, "user.human.password.check.failed")])
        recent = [i for i in r.items if i.key.startswith("auth.recent.")]
        self.assertEqual([i.values["kind"] for i in recent], ["password", "locked"])

    def test_only_search_api(self):
        _, http = self.run_fetch([])
        self.assertTrue(all(c["url"] == URL for c in http.calls))


class FalcoSourceTest(unittest.TestCase):
    def setUp(self):
        self.d, self.st = temp_store()
        self.ctx = make_ctx(store=self.st)

    def tearDown(self):
        self.st.close()
        self.d.cleanup()

    def add(self, rule, prio, minutes_ago, **f):
        self.st.insert_falco_event({"rule": rule, "priority": prio, "output": "out " + rule, "time": "t",
                                    "output_fields": {"k8s.ns.name": "ns", "k8s.pod.name": "p", **f}},
                                   NOW - timedelta(minutes=minutes_ago))

    def test_stats(self):
        self.add("A", "Warning", 10)
        self.add("A", "Notice", 10)
        self.add("B", "Warning", 60 * 30)
        r = security_falco.SOURCES[0].render(self.ctx, {"falco": ["24h"]})
        by = {i.key: i for i in r.items}
        self.assertEqual(len([k for k in by if k.startswith("falco.series.")]), 24)
        self.assertEqual(by["falco.series.2026-06-01T20"].values["warning"], 1)
        self.assertEqual(by["falco.series.2026-06-01T20"].values["notice"], 1)
        self.assertEqual(by["falco.rule.A"].values["count"], 2)
        self.assertNotIn("falco.rule.B", by)  # 24h の外
        r7 = security_falco.SOURCES[0].render(self.ctx, {"falco": ["7d"]})
        self.assertEqual(len([i for i in r7.items if i.key.startswith("falco.series.")]), 7)
        self.assertIn("falco.rule.B", {i.key for i in r7.items})
        recent = [i for i in r.items if i.key.startswith("falco.recent.")]
        self.assertEqual(recent[0].values["target"], "ns/p")

    def test_invalid_range_defaults(self):
        r = security_falco.SOURCES[0].render(self.ctx, {"falco": ["bogus"]})
        self.assertEqual(len([i for i in r.items if i.key.startswith("falco.series.")]), 24)

    def test_ingest_failed_surfaced(self):
        falco_ingest._failures = 3
        try:
            r = security_falco.SOURCES[0].render(self.ctx, {})
            i = {i.key: i for i in r.items}["falco.ingest_failed"]
            self.assertEqual((i.values["count"], i.status), (3, Status.WARN))
        finally:
            falco_ingest._failures = 0


class FalcoIngestTest(unittest.TestCase):
    def setUp(self):
        self.d, self.st = temp_store()
        self.srv = falco_ingest.make_falco_server(self.st, ("127.0.0.1", 0), "tok")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/falco"
        falco_ingest._failures = 0

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.st.close()
        self.d.cleanup()

    def post(self, body, token="tok", raw=False):
        h = {"Content-Type": "application/json"}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url, data=body if raw else json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_ok_saved(self):
        p = {"time": "t", "priority": "Warning", "rule": "R", "output": "o",
             "output_fields": {"k8s.ns.name": "n", "k8s.pod.name": "p", "container.name": "c"}}
        self.assertEqual(self.post(p), 204)
        row = self.st.query("SELECT * FROM falco_events")[0]
        self.assertEqual((row["rule"], row["k8s_ns"], row["k8s_pod"], row["container"]), ("R", "n", "p", "c"))

    def test_bad_token(self):
        self.assertEqual(self.post({"rule": "R"}, token="bad"), 401)
        self.assertEqual(self.post({"rule": "R"}, token=None), 401)
        self.assertEqual(self.st.query("SELECT count(*) FROM falco_events")[0][0], 0)

    def test_bad_format(self):
        self.assertEqual(self.post(b"{not json", raw=True), 400)
        self.assertEqual(self.post({"rule": 5}), 400)
        self.assertEqual(self.post([1]), 400)

    def test_empty_token_rejects_all(self):
        srv = falco_ingest.make_falco_server(self.st, ("127.0.0.1", 0), "")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/falco", data=b'{"rule":"R"}',
                                         headers={"Authorization": "Bearer "}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 401)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_store_failure_503_and_counted(self):
        class Broken:
            def insert_falco_event(self, *a):
                raise RuntimeError("disk")
        srv = falco_ingest.make_falco_server(Broken(), ("127.0.0.1", 0), "tok")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/falco", data=b'{"rule":"R"}',
                                         headers={"Authorization": "Bearer tok"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 503)
            self.assertEqual(falco_ingest.store_failures(), 1)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
