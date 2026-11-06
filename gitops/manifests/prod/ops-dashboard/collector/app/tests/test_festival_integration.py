"""取得結果の写し -> 評価 -> SQLite -> 断片、評価 -> 通知・ping を、実 SQLite とローカルの偽サーバーで通す。"""
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import main
import store as store_mod
from festival import page
from festival.evaluate import Evaluation, FestivalEvaluator
from festival.heartbeat import FestivalHeartbeat
from festival.notify import MAX_CHARS, FestivalNotifier, Notice
from helpers import make_ctx
from http_client import Http
from model import FestivalItem, Item, SourceResult, Status

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
CFG = config.load(Path(__file__).resolve().parents[2] / "dashboard.toml").festival
WORKER = Item("worker", "worker", Status.OK, {"requests": 1000, "errors": 80, "exceeded": 0, "cpu_p99_ms": 10,
                                               "window_end": "2026-06-01T11:57:00Z"})


class FakeServer:
    """受けた要求を (path, body) で記録し、scripted の (状態, ヘッダ) を順に返す。尽きたら 204。"""

    def __init__(self, scripted=()):
        self.requests, self.scripted = [], list(scripted)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.path, self.rfile.read(n)))
                code, headers = outer.scripted.pop(0) if outer.scripted else (204, {})
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = handle_any

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class FestivalFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))
        self.now = NOW
        self.servers = []
        self.col = main.Collector(make_ctx(store=self.store), self.store, [])
        self.col.results["festival.cloudflare"] = SourceResult(
            "festival.cloudflare", Status.OK, NOW, NOW, (WORKER,), None)

    def tearDown(self):
        for s in self.servers:
            s.close()
        self.store.close()
        self.tmp.cleanup()

    def server(self, *scripted):
        s = FakeServer(scripted)
        self.servers.append(s)
        return s

    def evaluator(self, discord=None, ping=None):
        http = Http()
        return FestivalEvaluator(
            CFG, self.col.latest, self.store,
            FestivalNotifier(CFG, self.store, http, discord.url + "/hook" if discord else None),
            lambda: self.now, FestivalHeartbeat(http, ping.url + "/ping" if ping else None, self.store),
            internal_networks=lambda: ())

    def test_evaluation_reaches_fragment_but_not_admin_page(self):
        self.col.results["festival.cloudflare"] = SourceResult("festival.cloudflare", Status.OK, NOW, NOW, (
            WORKER, Item("cms", "cms", Status.OK, {"requests": 1000, "status_5xx": 900, "origin_p95_ms": 100,
                                                   "window_end": "2026-06-01T11:57:00Z"})), None)
        self.evaluator().tick()
        frag = page.render_fragment(self.store, CFG, NOW)
        self.assertIn("festival-fragment", frag)
        crit = [i for i in self.store.load_festival_items() if i.status is Status.CRIT]
        self.assertTrue(crit)
        self.assertIn(crit[0].label, frag)
        web = main.make_web_server(self.col, ("127.0.0.1", 0))
        threading.Thread(target=web.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{web.server_address[1]}/admin/") as r:
                body = r.read().decode()
        finally:
            web.shutdown()
            web.server_close()
        self.assertNotIn("festival.", body)
        self.assertNotIn(crit[0].label, body)

    def _cms_cpu_over_minutes(self, ratios):
        ev = self.evaluator()
        for r in ratios:
            self.col.results["festival.workloads"] = SourceResult("festival.workloads", Status.OK, self.now, self.now, (
                Item("cms", "CMS", Status.OK, {"replicas": 1, "ready": 1, "restarts_recent": 0, "oom_recent": False,
                                               "crashloop": False, "cpu_ratio_max": r, "throttle_ratio_max": 0.0,
                                               "by_pod": [{"pod": "cms-0", "ready": True, "cpu_ratio": r,
                                                           "throttle_ratio": 0.0, "restarts_recent": 0,
                                                           "oom_recent": False, "crashloop": False}]}),), None)
            ev.tick()
            self.now += timedelta(minutes=1)
        self.now -= timedelta(minutes=1)

    @staticmethod
    def _points(svg):
        d = re.search(r'<path d="([^"]*)"', svg).group(1)
        return d.count("L") + 1

    def test_sparkline_reflects_several_minutes(self):
        self._cms_cpu_over_minutes([0.1, 0.5, 0.9])
        frag = page.render_fragment(self.store, CFG, self.now)
        tile = re.search(r'<[^>]*capacity\.cms\.cpu[^>]*>.*?<svg[^>]*class="spark".*?</svg>', frag, re.S)
        self.assertTrue(tile)
        self.assertEqual(self._points(tile.group(0)[tile.group(0).index("<svg"):]), 3)

    def test_keyed_fragment_has_day_chart_and_breakdown(self):
        self._cms_cpu_over_minutes([0.1, 0.5, 0.9])
        frag = page.render_fragment(self.store, CFG, self.now, key="capacity.cms.cpu")
        aside = frag[frag.index('<aside id="detail"'):]
        chart = re.search(r'<svg[^>]*class="daychart".*?</svg>', aside, re.S)
        self.assertTrue(chart)
        self.assertEqual(self._points(chart.group(0)), 3)
        self.assertIn("cms-0", aside)

    def test_429_waits_then_resends_once(self):
        d = self.server((429, {"Retry-After": "30"}))
        ev = self.evaluator(discord=d)
        ev.tick()
        self.assertEqual(len(d.requests), 1)
        self.now += timedelta(seconds=15)
        ev.tick()
        self.assertEqual(len(d.requests), 1)  # 待機中は送らない
        self.now += timedelta(seconds=16)
        ev.tick()
        self.assertEqual(len(d.requests), 2)
        self.assertEqual(d.requests[0][1], d.requests[1][1])  # 同じ通知を再送
        self.now += timedelta(seconds=60)
        ev.tick()
        self.assertEqual(len(d.requests), 2)  # 送達後は通知済み

    def test_simultaneous_alerts_are_one_message(self):
        d = self.server()
        self.col.results["festival.cloudflare"] = SourceResult("festival.cloudflare", Status.OK, NOW, NOW, (
            WORKER, Item("cms", "cms", Status.OK, {"requests": 1000, "status_5xx": 900, "origin_p95_ms": 12000,
                                                   "window_end": "2026-06-01T11:57:00Z"})), None)
        self.evaluator(discord=d).tick()
        self.assertEqual(len(d.requests), 1)
        content = json.loads(d.requests[0][1])["content"]
        self.assertGreaterEqual(content.count("🔴 異常:"), 2)
        self.assertEqual(content.count("🟡 注意: Worker エラー率"), 1)  # alert_on_warn の項目
        self.assertTrue(content.startswith("[当日監視] 異常 2 件・注意 1 件"))
        self.assertEqual(content.count("[当日監視]"), 1)

    def test_over_2000_chars_defers_rest_to_next_send(self):
        d = self.server()
        notifier = FestivalNotifier(CFG, self.store, Http(), d.url + "/hook")
        items = [FestivalItem(key=f"visitor.k{n}", category="visitor", label="長い名前" * 10, status=Status.CRIT,
                              value="9", threshold="異常 ≥ 1", observed_at=None, fetched_at=NOW,
                              source_ids=("festival.cloudflare",), link=None, note=None, notify=True, judged=True)
                 for n in range(40)]
        ev = Evaluation(NOW, items, [])
        first = notifier.send(notifier.plan(ev, self.store.load_alert_states(), NOW), NOW)
        self.assertTrue(first.sent and first.deferred)
        self.assertLessEqual(len(json.loads(d.requests[0][1])["content"]), MAX_CHARS)
        second = notifier.send(notifier.plan(ev, self.store.load_alert_states(), NOW), NOW)
        self.assertEqual({n.key for n in first.sent}.isdisjoint({n.key for n in second.sent}), True)
        self.assertEqual(len(d.requests), 2)

    def test_ping_interval_is_60_seconds(self):
        p = self.server()
        ev = self.evaluator(ping=p)
        for step in (0, 15, 30, 45, 60, 75):
            self.now = NOW + timedelta(seconds=step)
            ev.tick()
        self.assertEqual(len(p.requests), 2)
        self.assertIsNotNone(self.store.load_festival_state().heartbeat_ok_at)


if __name__ == "__main__":
    unittest.main()
