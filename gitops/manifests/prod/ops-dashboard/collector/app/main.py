import argparse
import json
import os
import signal
import sys
import threading
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Mapping
from urllib.parse import parse_qs, urlsplit

import config as config_mod
import falco_ingest
import model
import store as store_mod
from render import page as render_page
from http_client import Http
from k8s import K8sClient, K8sError
from model import FetchContext, Source, SourceResult, Status
from sources import load_sources

DEFAULT_TIMEOUT = 30.0
DAY = 86400


def log(event: str, **kv) -> None:
    print(json.dumps({"event": event, **kv}, ensure_ascii=False), flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Collector:
    """情報源ごとに独立したスレッドで取得し、最新結果をメモリと SQLite に保持する。"""

    def __init__(self, ctx: FetchContext, store, sources, clock=None, timeout: float = DEFAULT_TIMEOUT):
        self.ctx = replace(ctx, clock=clock) if clock else ctx
        self.clock = self.ctx.clock
        self.store = store
        self.sources = list(sources)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.results: dict[str, SourceResult] = store.load_results()

    def _execute(self, source: Source, fn, prev: SourceResult | None, *args) -> SourceResult:
        now = self.clock()
        box: list = []

        def target():
            try:
                box.append(fn(self.ctx, *args))
            except BaseException as e:
                box.append(e)

        # ponytail: タイムアウトした取得スレッドは止められず (標準ライブラリのみ)、HTTP のタイムアウトで自然に終わる
        t = threading.Thread(target=target, name=f"fetch-{source.source_id}", daemon=True)
        t.start()
        t.join(source.timeout or self.timeout)
        out = box[0] if box else TimeoutError(f"timeout after {source.timeout or self.timeout}s")

        if isinstance(out, SourceResult) and out.status != Status.ERROR:
            return replace(out, source_id=source.source_id, last_success_at=out.last_success_at or out.fetched_at)
        if isinstance(out, SourceResult):
            error, items, last = out.error, out.items, out.last_success_at
        else:
            error = model.safe_error(out if isinstance(out, BaseException) else TypeError("fetch returned non-result"))
            items, last = (), None
        log("fetch_failed", source=source.source_id, error=error)
        return SourceResult(source.source_id, Status.ERROR, now,
                            last or (prev.last_success_at if prev else None),
                            items or (prev.items if prev else ()), error)

    def collect(self, source: Source) -> SourceResult:
        with self._lock:
            prev = self.results.get(source.source_id)
        r = self._execute(source, source.fetch, prev)
        with self._lock:
            self.results[source.source_id] = r
        try:
            self.store.save_result(r)
        except Exception as e:
            log("save_failed", source=source.source_id, error=model.safe_error(e))
        return r

    def snapshot(self, query: Mapping[str, list[str]]) -> dict[str, SourceResult]:
        with self._lock:
            snap = dict(self.results)
        for s in self.sources:
            if s.render:
                snap[s.source_id] = self._execute(s, s.render, None, query)
        return snap

    def _loop(self, s: Source):
        while not self._stop.is_set():
            self.collect(s)
            self._stop.wait(s.interval.total_seconds())

    def _maintenance(self):
        while True:
            try:
                self.store.purge(self.clock())
            except Exception as e:
                log("purge_failed", error=model.safe_error(e))
            if self._stop.wait(DAY):
                return

    def start(self):
        for s in self.sources:
            if s.interval is not None:
                self._threads.append(threading.Thread(target=self._loop, args=(s,), name=f"src-{s.source_id}", daemon=True))
        self._threads.append(threading.Thread(target=self._maintenance, name="maintenance", daemon=True))
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1)


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_web_server(collector: Collector, addr, render=render_page.render) -> ThreadingHTTPServer:
    class Handler(_Quiet):
        def do_GET(self):
            u = urlsplit(self.path)
            if u.path == "/admin/healthz":
                return self.reply(200, b"ok")
            if u.path in ("/admin/", "/admin"):
                try:
                    page = self.render(collector.snapshot(parse_qs(u.query)), parse_qs(u.query))
                except Exception as e:
                    log("render_failed", error=model.safe_error(e))
                    return self.reply(500, b"render failed")
                return self.reply(200, page.encode(), "text/html; charset=utf-8")
            self.reply(404, b"not found")

    Handler.render = staticmethod(render)
    return ThreadingHTTPServer(addr, Handler)


def run(argv=None, env=None) -> int:
    env = dict(os.environ if env is None else env)
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=env.get("DASHBOARD_TOML", "/config/dashboard.toml"))
    ap.add_argument("--db", default=env.get("OPS_DB_PATH", "/data/collector.db"))
    ap.add_argument("--web-port", type=int, default=8080)
    ap.add_argument("--falco-port", type=int, default=8081)
    args = ap.parse_args(argv)

    try:
        cfg = config_mod.load(args.config)
    except config_mod.ConfigError as e:
        print(f"invalid config: {e}", file=sys.stderr)
        return 1

    store = store_mod.Store(args.db)
    try:
        k8s = K8sClient.in_cluster(env=env)
    except K8sError:
        k8s = None
    ctx = FetchContext(config=cfg, store=store, http=Http(), k8s=k8s, env=env)
    collector = Collector(ctx, store, load_sources())
    web = make_web_server(collector, ("", args.web_port))
    falco = falco_ingest.make_falco_server(store, ("", args.falco_port), env.get("OPS_FALCO_WEBHOOK_TOKEN", ""), log=log)
    threading.Thread(target=falco.serve_forever, name="falco-http", daemon=True).start()
    collector.start()
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=web.shutdown).start())
    log("started", web=args.web_port, falco=args.falco_port)
    try:
        web.serve_forever()
    finally:
        collector.stop()
        falco.shutdown()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(run())
