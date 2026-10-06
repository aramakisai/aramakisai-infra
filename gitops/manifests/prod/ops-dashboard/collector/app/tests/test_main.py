import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import falco_ingest
import main
import model
import store as store_mod
from helpers import FIXED_NOW, make_ctx
from model import Item, Source, SourceResult, Status

T0 = FIXED_NOW


def ok_result(sid, now, val=1):
    return SourceResult(sid, Status.OK, now, now, (Item("k", "L", Status.OK, {"v": val}, None),), None)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))
        self.clock = Clock()
        self.ctx = make_ctx(store=self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def collector(self, srcs, timeout=0.3):
        return main.Collector(self.ctx, self.store, srcs, clock=self.clock, timeout=timeout)

    def test_success_is_saved_and_persisted(self):
        s = Source("a", timedelta(minutes=1), lambda ctx: ok_result("a", ctx.now()))
        c = self.collector([s])
        r = c.collect(s)
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(c.results["a"], r)
        self.assertEqual(self.store.load_results()["a"], r)

    def test_exception_becomes_error_and_keeps_last_success(self):
        state = {"fail": False}

        def fetch(ctx):
            if state["fail"]:
                raise RuntimeError("boom https://x/y?token=SECRET")
            return ok_result("a", ctx.now(), 7)

        s = Source("a", timedelta(minutes=1), fetch)
        c = self.collector([s])
        good = c.collect(s)
        state["fail"] = True
        self.clock.t = T0 + timedelta(minutes=5)
        bad = c.collect(s)
        self.assertEqual(bad.status, Status.ERROR)
        self.assertEqual(bad.fetched_at, self.clock.t)
        self.assertEqual(bad.last_success_at, good.fetched_at)
        self.assertEqual(bad.items, good.items)
        self.assertNotIn("SECRET", bad.error)

    def test_error_without_previous_has_no_items(self):
        s = Source("a", timedelta(minutes=1), lambda ctx: 1 / 0)
        r = self.collector([s]).collect(s)
        self.assertEqual((r.status, r.items, r.last_success_at), (Status.ERROR, (), None))

    def test_returned_error_status_keeps_last_success(self):
        seq = iter([ok_result("a", T0), SourceResult("a", Status.ERROR, T0, None, (), "x")])
        s = Source("a", timedelta(minutes=1), lambda ctx: next(seq))
        c = self.collector([s])
        c.collect(s)
        r = c.collect(s)
        self.assertEqual(r.status, Status.ERROR)
        self.assertEqual(r.last_success_at, T0)
        self.assertEqual(len(r.items), 1)

    def test_timeout_does_not_block_and_is_error(self):
        release = threading.Event()
        s = Source("slow", timedelta(minutes=1), lambda ctx: (release.wait(5), ok_result("slow", ctx.now()))[1])
        c = self.collector([s], timeout=0.1)
        start = time.monotonic()
        r = c.collect(s)
        release.set()
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(r.status, Status.ERROR)
        self.assertIn("timeout", r.error)

    def test_one_failure_does_not_change_others(self):
        good = Source("good", timedelta(minutes=1), lambda ctx: ok_result("good", ctx.now()))
        bad = Source("bad", timedelta(minutes=1), lambda ctx: 1 / 0)
        c = self.collector([good, bad])
        before = c.collect(good)
        c.collect(bad)
        self.assertEqual(c.results["good"], before)
        self.assertEqual(c.results["bad"].status, Status.ERROR)
        page = main.render_page.render(c.snapshot({}), {})
        self.assertIn("bad", page)
        self.assertIn("取得失敗", page)

    def test_render_time_sources_use_query(self):
        seen = []

        def render(ctx, query):
            seen.append(query)
            return ok_result("r", ctx.now())

        s = Source("r", None, None, render=render)
        c = self.collector([s])
        snap = c.snapshot({"falco": ["7d"]})
        self.assertEqual(seen, [{"falco": ["7d"]}])
        self.assertEqual(snap["r"].status, Status.OK)

    def test_render_time_failure_isolated(self):
        s = Source("r", None, None, render=lambda ctx, q: 1 / 0)
        snap = self.collector([s]).snapshot({})
        self.assertEqual(snap["r"].status, Status.ERROR)

    def test_loads_persisted_results_on_start(self):
        self.store.save_result(ok_result("a", T0))
        s = Source("a", timedelta(minutes=1), lambda ctx: ok_result("a", ctx.now()))
        self.assertIn("a", self.collector([s]).results)

    def test_scheduler_runs_and_stops(self):
        n = []
        s = Source("a", timedelta(milliseconds=20), lambda ctx: (n.append(1), ok_result("a", ctx.now()))[1])
        c = self.collector([s])
        c.start()
        time.sleep(0.2)
        c.stop()
        self.assertGreaterEqual(len(n), 2)


def req(url, method="GET", data=None, headers=None):
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        body = e.read()
        e.close()
        return e.code, body


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))
        ctx = make_ctx(store=self.store)
        self.sources = [Source("good", timedelta(minutes=1), lambda c: ok_result("good", c.now()))]
        self.col = main.Collector(ctx, self.store, self.sources)
        self.col.collect(self.sources[0])
        self.web = main.make_web_server(self.col, ("127.0.0.1", 0), render=lambda snap, q: "PAGE " + ",".join(snap))
        self.falco = falco_ingest.make_falco_server(self.store, ("127.0.0.1", 0), token="s3cret")
        for srv in (self.web, self.falco):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.w = f"http://127.0.0.1:{self.web.server_address[1]}"
        self.f = f"http://127.0.0.1:{self.falco.server_address[1]}"

    def tearDown(self):
        for srv in (self.web, self.falco):
            srv.shutdown()
            srv.server_close()
        self.store.close()
        self.tmp.cleanup()

    def test_healthz(self):
        self.assertEqual(req(self.w + "/admin/healthz")[0], 200)

    def test_page(self):
        code, body = req(self.w + "/admin/?falco=7d")
        self.assertEqual((code, body), (200, b"PAGE good"))

    def test_unknown_path(self):
        self.assertEqual(req(self.w + "/other")[0], 404)

    def test_render_failure_is_500(self):
        self.web.RequestHandlerClass.render = staticmethod(lambda snap, q: 1 / 0)
        self.assertEqual(req(self.w + "/admin/")[0], 500)

    def falco_post(self, body, token="s3cret"):
        h = {"Content-Type": "application/json"}
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        return req(self.f + "/falco", "POST", body, h)[0]

    def test_falco_ok(self):
        payload = json.dumps({"time": "2026-06-01T00:00:00Z", "priority": "Warning", "rule": "r", "output": "o",
                              "output_fields": {"k8s.ns.name": "ns"}}).encode()
        self.assertEqual(self.falco_post(payload), 204)
        self.assertEqual(self.store.query("SELECT rule,k8s_ns FROM falco_events")[0][0], "r")

    def test_falco_bad_token(self):
        self.assertEqual(self.falco_post(b"{}", token="wrong"), 401)
        self.assertEqual(self.falco_post(b"{}", token=None), 401)
        self.assertEqual(self.store.query("SELECT COUNT(*) FROM falco_events")[0][0], 0)

    def test_falco_bad_body(self):
        self.assertEqual(self.falco_post(b"not json"), 400)
        self.assertEqual(self.falco_post(b'{"no_rule": 1}'), 400)
        self.assertEqual(self.falco_post(b"[1]"), 400)

    def test_falco_other_path_404(self):
        self.assertEqual(req(self.f + "/x", "POST", b"{}", {"Authorization": "Bearer s3cret"})[0], 404)

    def test_falco_empty_token_config_rejects_all(self):
        srv = falco_ingest.make_falco_server(self.store, ("127.0.0.1", 0), token="")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/falco"
            self.assertEqual(req(url, "POST", b'{"rule":"r"}', {"Authorization": "Bearer "})[0], 401)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_falco_store_failure_503(self):
        self.store.write(lambda c: c.execute("DROP TABLE falco_events"))
        self.assertEqual(self.falco_post(b'{"rule":"r","output":"o"}'), 503)


class MainConfigTest(unittest.TestCase):
    def test_bad_config_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.toml")
            open(p, "w").write("[[plans")
            self.assertNotEqual(main.run(["--config", p, "--db", os.path.join(d, "db")], env={}), 0)


if __name__ == "__main__":
    unittest.main()
