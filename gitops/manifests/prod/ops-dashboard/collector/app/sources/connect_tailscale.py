from datetime import timedelta

import model
from model import Item, Source, Status

API = "https://api.tailscale.com/api/v2"


def fetch(ctx):
    # 既定の tailnet は "-" (資格情報が属する tailnet)。
    tailnet = ctx.config.source_settings("connect.tailscale").get("tailnet", "-")
    tok = ctx.http.post_form(f"{API}/oauth/token", {
        "grant_type": "client_credentials",
        "client_id": ctx.secret("OPS_TAILSCALE_OAUTH_CLIENT_ID"),
        "client_secret": ctx.secret("OPS_TAILSCALE_OAUTH_CLIENT_SECRET")})["access_token"]
    devices = ctx.http.get_json(f"{API}/tailnet/{tailnet}/devices", params={"fields": "all"}, bearer=tok)["devices"]
    items = []
    for d in devices:
        host = d.get("hostname") or d.get("name") or d.get("id")
        online = bool(d.get("connectedToControl"))
        # タグ付きはサーバー用途なのでオフラインを要確認にする。個人端末のオフラインは通常の状態。
        status = Status.WARN if not online and d.get("tags") else Status.OK
        items.append(Item(f"device.{host}", host, status, {"online": int(online), "last_seen": d.get("lastSeen")}))
    return model.make_result("connect.tailscale", ctx.now(), items)


SOURCES = (Source("connect.tailscale", timedelta(minutes=5), fetch),)
