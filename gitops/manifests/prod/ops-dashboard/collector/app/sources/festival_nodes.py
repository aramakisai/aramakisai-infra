import ipaddress
from datetime import timedelta

from http_client import HttpError
from model import Item, Source, Status, make_result
from render.labels import label
from sources._common import condition
from sources.node_resources import quantity

SOURCE_ID = "festival.nodes"

# fail2ban の照合用。IP なので Item には入れず、評価器が internal_networks() で受け取る。
_networks: tuple = ()
# ノード名 -> (取得時刻, rxBytes, txBytes)。kubelet のカウンタは累積値なので前回との差でレートを出す
_net_prev: dict = {}


def internal_networks() -> tuple:
    """直近の取得で得たノードのアドレス (/32・/128) と podCIDRs。未取得なら空。"""
    return _networks


def _addresses(n) -> list:
    out = []
    for a in n["status"].get("addresses", []):
        try:
            out.append(ipaddress.ip_address(a["address"]))
        except ValueError:
            pass  # Hostname など
    return out


def _ratio(usage, capacity):
    return round(quantity(usage) / quantity(capacity), 4) if usage and quantity(capacity) else None


def _summary(ctx, n):
    ip = next((a["address"] for a in n["status"].get("addresses", []) if a["type"] == "InternalIP"), None)
    try:
        return ctx.k8s.kubelet_stats(ip)["node"]
    except (HttpError, KeyError, TypeError):
        return None


def _disk(node):
    try:
        fs = node["fs"]
        return round(fs["usedBytes"] / fs["capacityBytes"], 4)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def _net_rates(name, node, now):
    """(rx, tx) のバイト毎秒。初回・カウンタ減少 (再起動)・値欠落は None。"""
    try:
        cur = (now, node["network"]["rxBytes"], node["network"]["txBytes"])
    except (KeyError, TypeError):
        _net_prev.pop(name, None)
        return None, None
    prev = _net_prev.get(name)
    _net_prev[name] = cur
    secs = (now - prev[0]).total_seconds() if prev else 0
    if secs <= 0 or cur[1] < prev[1] or cur[2] < prev[2]:
        return None, None
    return (cur[1] - prev[1]) / secs, (cur[2] - prev[2]) / secs


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or SOURCE_ID in fc.disabled_sources:
        return make_result(SOURCE_ID, ctx.now(), [], Status.EMPTY)
    nodes = ctx.k8s.get("/api/v1/nodes")["items"]
    usage = {m["metadata"]["name"]: m["usage"] for m in ctx.k8s.get("/apis/metrics.k8s.io/v1beta1/nodes")["items"]}
    items, nets = [], []
    for n in nodes:
        name = n["metadata"]["name"]
        u = usage.get(name, {})
        cap = n["status"]["capacity"]
        summary = _summary(ctx, n)
        disk = _disk(summary)
        rx, tx = _net_rates(name, summary, ctx.now())
        addrs = _addresses(n)
        nets += [ipaddress.ip_network(a) for a in addrs]
        nets += [ipaddress.ip_network(c) for c in n.get("spec", {}).get("podCIDRs", [])]
        items.append(Item(f"node.{name}", name, Status.OK,
                          {"ready": (condition(n, "Ready") or {}).get("status") == "True",
                           "cpu_ratio": _ratio(u.get("cpu"), cap["cpu"]),
                           "memory_ratio": _ratio(u.get("memory"), cap["memory"]),
                           "disk_ratio": disk, "addresses_count": len(addrs),
                           "net_rx_bps": rx, "net_tx_bps": tx},
                          label("note.metrics_missing") if disk is None or not u else None))
    global _networks
    _networks = tuple(nets)
    return make_result(SOURCE_ID, ctx.now(), items)


SOURCES = (Source(SOURCE_ID, timedelta(seconds=60), fetch),)
