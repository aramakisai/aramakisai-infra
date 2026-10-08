import os
from datetime import timedelta

from model import Item, Source, Status, make_result
from render.labels import label

STATE_DIR = "/var/lib/ops-dashboard"
# collector は hostPath と local-path PVC のため prod-node-1 に固定 (deployment.yaml の nodeSelector)。
# ディスク使用率と node-status.json はこのノードのものしか読めない。
COLLECTOR_NODE = "prod-node-1"
_SUFFIX = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12,
           "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def quantity(s: str) -> float:
    """Kubernetes の数量表記 (250m・123456789n・8Gi 等) を基本単位の数値にする。"""
    i = len(s)
    while i and not (s[i - 1].isdigit() or s[i - 1] == "."):
        i -= 1
    return float(s[:i]) * _SUFFIX[s[i:]]


def _ratio_item(key, label_, status_for, used, total):
    ratio = used / total if total else 0.0
    status, note = status_for(ratio)
    return Item(key, label_, status, {"ratio": round(ratio * 100, 1), "used": used, "total": total}, note)


def fetch(ctx):
    th = ctx.config.thresholds
    wanted = {s.name for s in ctx.config.servers}
    nodes = [n for n in ctx.k8s.get("/api/v1/nodes")["items"] if n["metadata"]["name"] in wanted]
    metrics = {m["metadata"]["name"]: m["usage"]
               for m in ctx.k8s.get("/apis/metrics.k8s.io/v1beta1/nodes")["items"]}

    def mem(r):
        return (Status.WARN, None) if r >= th.memory_warn else (Status.OK, None)

    def disk(r):
        if r >= th.disk_crit:
            return Status.CRIT, label("note.disk_alert")
        return (Status.WARN, label("note.disk_alert")) if r > th.disk_warn else (Status.OK, None)

    items = []
    for n in nodes:
        name = n["metadata"]["name"]
        usage, cap = metrics.get(name), n["status"]["capacity"]
        if usage is None:
            # 再起動直後のノードは metrics-server に現れないことがある。他ノードの表示は残す。
            items += [Item(f"{k}:{name}", name, Status.STALE, {}, label("note.metrics_missing"))
                      for k in ("cpu", "memory")]
            continue
        items += [
            _ratio_item(f"cpu:{name}", name, lambda r: (Status.OK, None), quantity(usage["cpu"]), quantity(cap["cpu"])),
            _ratio_item(f"memory:{name}", name, mem, quantity(usage["memory"]), quantity(cap["memory"])),
        ]
    st = os.statvfs(ctx.config.source_settings("node.resources").get("state_dir", STATE_DIR))
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    # df と同じ基準 (used / (used + avail))。Discord 通知の使用率と揃える。
    total = used + st.f_bavail * st.f_frsize
    items.append(_ratio_item("disk", COLLECTOR_NODE, disk, used, total))
    return make_result("node.resources", ctx.now(), items)


SOURCES = (Source("node.resources", timedelta(minutes=1), fetch),)
