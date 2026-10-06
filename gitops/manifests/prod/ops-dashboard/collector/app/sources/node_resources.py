import os
from datetime import timedelta

from model import Item, Source, Status, make_result
from render.labels import label

STATE_DIR = "/var/lib/ops-dashboard"
_SUFFIX = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
           "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def quantity(s: str) -> float:
    """Kubernetes の数量表記 (250m・123456789n・8Gi 等) を基本単位の数値にする。"""
    i = len(s)
    while i and not (s[i - 1].isdigit() or s[i - 1] == "."):
        i -= 1
    return float(s[:i]) * _SUFFIX[s[i:]]


def _ratio_item(key, status_for, used, total):
    ratio = used / total if total else 0.0
    status, note = status_for(ratio)
    return Item(key, key, status, {"ratio": round(ratio * 100, 1), "used": used, "total": total}, note)


def fetch(ctx):
    th = ctx.config.thresholds
    nodes = ctx.k8s.get("/api/v1/nodes")["items"]
    node = next((n for n in nodes if n["metadata"]["name"] in {s.name for s in ctx.config.servers}), nodes[0])
    name = node["metadata"]["name"]
    usage = next(m for m in ctx.k8s.get("/apis/metrics.k8s.io/v1beta1/nodes")["items"]
                 if m["metadata"]["name"] == name)["usage"]
    cap = node["status"]["capacity"]

    def mem(r):
        return (Status.WARN, None) if r >= th.memory_warn else (Status.OK, None)

    def disk(r):
        if r >= th.disk_crit:
            return Status.CRIT, label("note.disk_alert")
        return (Status.WARN, label("note.disk_alert")) if r > th.disk_warn else (Status.OK, None)

    st = os.statvfs(ctx.config.source_settings("node.resources").get("state_dir", STATE_DIR))
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    # df と同じ基準 (used / (used + avail))。Discord 通知の使用率と揃える。
    total = used + st.f_bavail * st.f_frsize
    items = [
        _ratio_item("cpu", lambda r: (Status.OK, None), quantity(usage["cpu"]), quantity(cap["cpu"])),
        _ratio_item("memory", mem, quantity(usage["memory"]), quantity(cap["memory"])),
        _ratio_item("disk", disk, used, total),
    ]
    return make_result("node.resources", ctx.now(), [
        Item(i.key, name, i.status, i.values, i.note) for i in items])


SOURCES = (Source("node.resources", timedelta(minutes=1), fetch),)
