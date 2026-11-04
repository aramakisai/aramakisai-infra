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


def _disk(ctx, n):
    ip = next((a["address"] for a in n["status"].get("addresses", []) if a["type"] == "InternalIP"), None)
    try:
        fs = ctx.k8s.kubelet_stats(ip)["node"]["fs"]
        return round(fs["usedBytes"] / fs["capacityBytes"], 4)
    except (HttpError, KeyError, TypeError, ZeroDivisionError):
        return None


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
        disk = _disk(ctx, n)
        addrs = _addresses(n)
        nets += [ipaddress.ip_network(a) for a in addrs]
        nets += [ipaddress.ip_network(c) for c in n.get("spec", {}).get("podCIDRs", [])]
        items.append(Item(f"node.{name}", name, Status.OK,
                          {"ready": (condition(n, "Ready") or {}).get("status") == "True",
                           "cpu_ratio": _ratio(u.get("cpu"), cap["cpu"]),
                           "memory_ratio": _ratio(u.get("memory"), cap["memory"]),
                           "disk_ratio": disk, "addresses_count": len(addrs)},
                          label("note.metrics_missing") if disk is None or not u else None))
    global _networks
    _networks = tuple(nets)
    return make_result(SOURCE_ID, ctx.now(), items)


SOURCES = (Source(SOURCE_ID, timedelta(seconds=60), fetch),)
