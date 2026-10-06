from datetime import timedelta

import model
from model import Item, Source, Status

API = "https://api.cloudflare.com/client/v4"
_STATUS = {"healthy": Status.OK, "degraded": Status.WARN, "down": Status.CRIT, "inactive": Status.WARN}


def _result(body):
    if not body.get("success", False):
        raise RuntimeError("cloudflare api returned success=false")
    return body["result"]


def fetch(ctx):
    s = ctx.config.source_settings("connect.tunnel")
    token = ctx.secret("OPS_CLOUDFLARE_READ_TOKEN")
    base = f"{API}/accounts/{s['account_id']}/cfd_tunnel/{s['tunnel_id']}"
    state = _result(ctx.http.get_json(base, bearer=token))["status"]
    # tunnel 本体の connections は非推奨で空配列を返すため、接続数は /connections から数える。
    conns = sum(len(c.get("conns") or []) for c in _result(ctx.http.get_json(f"{base}/connections", bearer=token)))
    return model.make_result("connect.tunnel", ctx.now(),
                             [Item("tunnel", "tunnel", _STATUS.get(state, Status.WARN),
                                   {"state": state, "connections": conns})])


SOURCES = (Source("connect.tunnel", timedelta(minutes=2), fetch),)
