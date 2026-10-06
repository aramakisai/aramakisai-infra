"""情報源のテストで fetch(ctx) に渡す FetchContext を組み立てる共通部品。"""
import os
import tempfile
from datetime import datetime, timezone

import config
import http_client
from model import FetchContext

FIXED_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


class FakeHttp:
    """routes は URL (クエリなし) -> 応答。応答が Exception なら raise、callable なら呼び出して返す。
    get_json / post_json / post_form / get_bytes のどれで呼んでも同じ routes を引く。呼び出しは calls に残る。"""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def _do(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if url not in self.routes:
            raise http_client.HttpError(f"{method} {url} -> no route", status=404)
        r = self.routes[url]
        if isinstance(r, Exception):
            raise r
        return r(**kw) if callable(r) else r

    def get_json(self, url, **kw):
        return self._do("GET", url, **kw)

    def get_bytes(self, url, **kw):
        return self._do("GET", url, **kw)

    def post_json(self, url, body, **kw):
        return self._do("POST", url, body=body, **kw)

    def post_form(self, url, data, **kw):
        return self._do("POST", url, data=data, **kw)


class FakeK8s:
    """paths は API パス -> JSON (dict)。list_items は paths[path]["items"] を exclude 適用して返す。"""

    def __init__(self, paths=None):
        self.paths = dict(paths or {})

    def get(self, path, **kw):
        if path not in self.paths:
            raise KeyError(path)
        return self.paths[path]

    def list_items(self, path, exclude=()):
        return [i for i in self.get(path)["items"]
                if i["metadata"].get("namespace") not in exclude and i["metadata"].get("name") not in exclude]


def make_ctx(cfg=None, http=None, k8s=None, env=None, store=None, now=FIXED_NOW):
    """cfg 省略時は本番の dashboard.toml。store 省略時は一時 DB は作らず None。"""
    if cfg is None:
        cfg = config.load(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard.toml"))
    return FetchContext(config=cfg, store=store, http=http or FakeHttp(), k8s=k8s or FakeK8s(),
                        env=dict(env or {}), clock=lambda: now)


def temp_store():
    import store as store_mod
    d = tempfile.TemporaryDirectory()
    return d, store_mod.Store(os.path.join(d.name, "t.db"))
