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
    account = ctx.secret("TF_VAR_cloudflare_account_id")
    # tunnel ID は terraform output で公開リポジトリに書かないため、名前から引く
    tunnels = _result(ctx.http.get_json(f"{API}/accounts/{account}/cfd_tunnel",
                                        params={"name": s["tunnel_name"], "is_deleted": "false"}, bearer=token))
    if not tunnels:
        raise RuntimeError(f"tunnel not found: {s['tunnel_name']}")
    base = f"{API}/accounts/{account}/cfd_tunnel/{tunnels[0]['id']}"
    state = _result(ctx.http.get_json(base, bearer=token))["status"]
    # tunnel 本体の connections は非推奨で空配列を返すため、接続数は /connections から数える。
    conns = sum(len(c.get("conns") or []) for c in _result(ctx.http.get_json(f"{base}/connections", bearer=token)))
    return model.make_result("connect.tunnel", ctx.now(),
                             [Item("tunnel", "tunnel", _STATUS.get(state, Status.WARN),
                                   {"state": state, "connections": conns})])


SOURCES = (Source("connect.tunnel", timedelta(minutes=2), fetch),)
