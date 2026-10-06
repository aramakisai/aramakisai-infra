import hmac
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import model

MAX_BODY = 1_000_000

_lock = threading.Lock()
_failures = 0


def store_failures() -> int:
    """プロセス起動後に保存へ失敗した件数。再起動で 0 に戻る。"""
    with _lock:
        return _failures


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_falco_server(store, addr, token: str, clock=_utcnow, log=lambda event, **kv: None) -> ThreadingHTTPServer:
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code: int):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            # トークン未設定時は全拒否 (空の Bearer を通さない)。不正トークンはログに残さない。
            got = (self.headers.get("Authorization") or "").encode()
            if not token or not hmac.compare_digest(got, expected):
                return self.reply(401)
            if urlsplit(self.path).path != "/falco":
                return self.reply(404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n <= MAX_BODY:
                    return self.reply(400)
                payload = json.loads(self.rfile.read(n))
            except ValueError:
                return self.reply(400)
            if not isinstance(payload, dict) or not isinstance(payload.get("rule"), str):
                return self.reply(400)
            try:
                store.insert_falco_event(payload, clock())
            except Exception as e:
                global _failures
                with _lock:
                    _failures += 1
                log("falco_store_failed", error=model.safe_error(e))
                return self.reply(503)
            self.reply(204)

    return ThreadingHTTPServer(addr, Handler)
