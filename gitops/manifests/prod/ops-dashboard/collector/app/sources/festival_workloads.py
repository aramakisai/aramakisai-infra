from datetime import timedelta
from typing import NamedTuple

from model import Item, Source, Status, make_result, parse_iso
from sources._common import condition
from sources.node_resources import quantity

SOURCE_ID = "festival.workloads"


class DbEndpoint(NamedTuple):
    pod: str
    ip: str
    role: str


# festival.cnpg が 9187 番の取得先に使う。Pod IP は Item に入れず、このモジュール内の最新値として持つ。
_db_endpoints: tuple[DbEndpoint, ...] = ()


def db_endpoints() -> tuple[DbEndpoint, ...]:
    return _db_endpoints


def _pods(ctx, ns, selector):
    items = ctx.k8s.get(f"/api/v1/namespaces/{ns}/pods", params={"labelSelector": selector})["items"]
    return [p for p in items if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]


def _cpu_usage(ctx, ns, selector):
    items = ctx.k8s.get(f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods", params={"labelSelector": selector})["items"]
    return {m["metadata"]["name"]: {c["name"]: quantity(c["usage"]["cpu"]) for c in m.get("containers", [])}
            for m in items}


def _cpu_ratio(pod, usage):
    """コンテナごとの CPU 使用量 ÷ limit の最大値。limit か使用量が無ければ None。"""
    used = usage.get(pod["metadata"]["name"])
    if used is None:
        return None
    ratios = [used[c["name"]] / quantity(c["resources"]["limits"]["cpu"])
              for c in pod["spec"]["containers"] if "cpu" in c.get("resources", {}).get("limits", {}) and c["name"] in used]
    return round(max(ratios), 4) if ratios else None


def _max(values):
    values = [v for v in values if v is not None]
    return max(values) if values else None


def _events(ctx, pod):
    """窓内の再起動数・OOM の有無・CrashLoop の有無。lastState は次の再起動まで残るので終了時刻で窓を切る。"""
    window, now = ctx.config.festival.cms.restart_window, ctx.now()
    restarts, oom, crash = 0, False, False
    for cs in pod.get("status", {}).get("containerStatuses", []):
        if cs.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff":
            crash = True
        term = cs.get("lastState", {}).get("terminated") or {}
        if term.get("finishedAt") and now - parse_iso(term["finishedAt"]) <= window:
            restarts += 1
            oom = oom or term.get("reason") == "OOMKilled"
    return restarts, oom, crash


def _ready(pod):
    return (condition(pod, "Ready") or {}).get("status") == "True"


def _cms(ctx, fc):
    c = fc.cms
    replicas = ctx.k8s.get(f"/apis/apps/v1/namespaces/{c.namespace}/deployments/{c.deployment}")["spec"]["replicas"]
    pods = _pods(ctx, c.namespace, c.pod_selector)
    usage = _cpu_usage(ctx, c.namespace, c.pod_selector)
    ev = [_events(ctx, p) for p in pods]
    return Item("cms", "CMS", Status.OK, {
        "replicas": replicas, "ready": sum(_ready(p) for p in pods),
        "restarts_recent": sum(e[0] for e in ev), "oom_recent": any(e[1] for e in ev),
        "crashloop": any(e[2] for e in ev), "cpu_ratio_max": _max(_cpu_ratio(p, usage) for p in pods)})


def _db(ctx, fc):
    global _db_endpoints
    c = fc.cms
    sel = f"cnpg.io/cluster={c.db_cluster},cnpg.io/podRole=instance"
    primary = ctx.k8s.get(f"/apis/postgresql.cnpg.io/v1/namespaces/{c.namespace}/clusters/{c.db_cluster}"
                          )["status"].get("currentPrimary")
    pods = _pods(ctx, c.namespace, sel)
    usage = _cpu_usage(ctx, c.namespace, sel)
    items, eps = [], []
    for p in sorted(pods, key=lambda p: p["metadata"]["name"]):
        name = p["metadata"]["name"]
        role = "primary" if name == primary else "replica"
        restarts, oom, crash = _events(ctx, p)
        items.append(Item(f"db.{name}", name, Status.OK, {
            "role": role, "cpu_ratio": _cpu_ratio(p, usage),
            "restarts_recent": restarts, "oom_recent": oom, "crashloop": crash}))
        if p["status"].get("podIP"):
            eps.append(DbEndpoint(name, p["status"]["podIP"], role))
    _db_endpoints = tuple(eps)
    return items


def _cloudflared(ctx, fc):
    c = fc.cloudflared
    pods = _pods(ctx, c.namespace, c.pod_selector)
    usage = _cpu_usage(ctx, c.namespace, c.pod_selector)
    return Item("cloudflared", "cloudflared", Status.OK,
                {"pods": len(pods), "cpu_ratio_max": _max(_cpu_ratio(p, usage) for p in pods)})


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or SOURCE_ID in fc.disabled_sources:
        return make_result(SOURCE_ID, ctx.now(), [], Status.EMPTY)
    return make_result(SOURCE_ID, ctx.now(), [_cms(ctx, fc), *_db(ctx, fc), _cloudflared(ctx, fc)])


SOURCES = (Source(SOURCE_ID, timedelta(seconds=30), fetch),)
