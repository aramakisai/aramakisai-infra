from datetime import timedelta

from model import Item, Source, Status, make_result
from plan import hetzner_estimate, jst_today, server_diff
from render.labels import label

API = "https://api.hetzner.cloud/v1"


def _paged(ctx, url, key, token):
    out, page = [], 1
    while page:
        d = ctx.http.get_json(url, params={"page": str(page), "per_page": "50"}, bearer=token)
        out += d[key]
        page = (d.get("meta") or {}).get("pagination", {}).get("next_page")
    return out


def fetch(ctx):
    token = ctx.secret("OPS_HCLOUD_READ_TOKEN")
    now = ctx.now()
    servers = _paged(ctx, f"{API}/servers", "servers", token)
    pricing = ctx.http.get_json(f"{API}/pricing", bearer=token)["pricing"]
    prices = {(t["name"], p["location"]): p for t in pricing["server_types"] for p in t["prices"]}
    os_plan = ctx.config.active_plan("hetzner_object_storage", jst_today(now))
    os_fee = float(os_plan.limits.get("price_month", 0)) if os_plan else 0.0
    est = hetzner_estimate(servers, prices, now, os_fee)
    unexpected, missing = server_diff(ctx.config, servers, now)
    by_name = {s["name"]: s for s in servers}

    items = []
    for s in servers:
        bad = s["name"] in unexpected or s["name"] in missing
        items.append(Item(
            f"hetzner.server.{s['name']}", s["name"], Status.CRIT if bad else Status.OK,
            {"type": s["server_type"]["name"], "location": s["location"]["name"], "created": s["created"],
             "state": s["status"], "outgoing_bytes": s.get("outgoing_traffic") or 0,
             "included_bytes": s.get("included_traffic") or 0, "cost": est["per_server"].get(s["name"])},
            label("note.server.unexpected") if s["name"] in unexpected else
            label("note.server.missing") if s["name"] in missing else None))
    for n in missing:
        if n not in by_name:
            items.append(Item(f"hetzner.server.{n}", n, Status.CRIT, {"state": "absent"},
                              label("note.server.missing")))
    out = sum(s.get("outgoing_traffic") or 0 for s in servers)
    inc = sum(s.get("included_traffic") or 0 for s in servers)
    items.append(Item("hetzner.estimate", label("sub.hetzner"), Status.OK,
                      {"amount": est["total"], "currency": "EUR", "outgoing_bytes": out, "included_bytes": inc}))
    return make_result("billing.hetzner", now, items)


SOURCES = (Source("billing.hetzner", timedelta(hours=1), fetch),)
