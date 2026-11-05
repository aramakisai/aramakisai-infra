from datetime import timedelta
from typing import NamedTuple

from http_client import HttpError
from model import Item, Source, Status, make_result, parse_iso
from render.labels import label
from sources import _promtext
from sources._common import condition
from sources.node_resources import quantity

SOURCE_ID = "festival.workloads"


class DbEndpoint(NamedTuple):
    pod: str
    ip: str
    role: str


# festival.cnpg が 9187 番の取得先に使う。Pod IP は Item に入れず、このモジュール内の最新値として持つ。
_db_endpoints: tuple[DbEndpoint, ...] = ()


# (namespace, pod, container) -> (throttled_periods, periods)。スロットリング率は前回との差分で求める。
_prev_cfs: dict[tuple[str, str, str], tuple[float, float]] = {}
_THROTTLED, _PERIODS = "container_cpu_cfs_throttled_periods_total", "container_cpu_cfs_periods_total"


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


def _kubelet(cache, key, call):
    """ノードごとの kubelet 応答を 1 周期内で使い回す。届かなければ None (そのノードの Pod の値だけ欠かす)。"""
    if key not in cache:
        try:
            cache[key] = call()
        except (HttpError, KeyError, ValueError):
            cache[key] = None
    return cache[key]


def _throttle_ratios(ctx, cache, pods):
    """Pod 名 -> コンテナごとのスロットリング率の最大値。計算できない Pod は含めない。"""
    global _prev_cfs
    out, new = {}, {}
    for ip in {p["status"].get("hostIP") for p in pods}:
        text = _kubelet(cache, ("cadvisor", ip), lambda: ctx.k8s.kubelet_text(ip, "/metrics/cadvisor"))
        if text is None:
            continue
        s = _promtext.parse_samples(text, (_THROTTLED, _PERIODS))
        periods = {(l.get("namespace"), l.get("pod"), l.get("container")): v for l, v in s[_PERIODS] if l.get("container")}
        for l, thr in s[_THROTTLED]:
            key = (l.get("namespace"), l.get("pod"), l.get("container"))
            if not key[2] or key not in periods:
                continue
            new[key] = (thr, periods[key])
    for p in pods:
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        for key, (thr, per) in new.items():
            if key[:2] != (ns, name):
                continue
            prev = _prev_cfs.get(key)
            if prev is None or thr < prev[0] or per < prev[1]:
                continue
            ratio = 0.0 if per == prev[1] else (thr - prev[0]) / (per - prev[1])
            out[name] = max(out.get(name, 0.0), ratio)
    live = {(p["metadata"]["namespace"], p["metadata"]["name"]) for p in pods}
    # 取得できなかったノードの Pod の前回値は残し、消えた Pod の分だけ捨てる
    _prev_cfs = {k: v for k, v in {**_prev_cfs, **new}.items() if k[:2] in live}
    return out


def _throttle_max(pods, ratios):
    v = _max(ratios.get(p["metadata"]["name"]) for p in pods)
    return v, (label("note.metrics_missing") if v is None and pods else None)


def _cms(ctx, fc, pods, ratios):
    c = fc.cms
    replicas = ctx.k8s.get(f"/apis/apps/v1/namespaces/{c.namespace}/deployments/{c.deployment}")["spec"]["replicas"]
    usage = _cpu_usage(ctx, c.namespace, c.pod_selector)
    ev = [_events(ctx, p) for p in pods]
    throttle, note = _throttle_max(pods, ratios)
    by_pod = [{"pod": p["metadata"]["name"], "ready": _ready(p), "cpu_ratio": _cpu_ratio(p, usage),
               "throttle_ratio": ratios.get(p["metadata"]["name"]), "restarts_recent": e[0],
               "oom_recent": e[1], "crashloop": e[2]} for p, e in zip(pods, ev)]
    return Item("cms", "CMS", Status.OK, {
        "replicas": replicas, "ready": sum(_ready(p) for p in pods),
        "restarts_recent": sum(e[0] for e in ev), "oom_recent": any(e[1] for e in ev),
        "crashloop": any(e[2] for e in ev), "cpu_ratio_max": _max(_cpu_ratio(p, usage) for p in pods),
        "throttle_ratio_max": throttle, "by_pod": by_pod}, note)


def _rss_ratio(pod, stats):
    """コンテナごとの rssBytes ÷ memory limit の最大値。ページキャッシュを含む workingSet は使わない。"""
    if stats is None:
        return None
    ref = (pod["metadata"]["namespace"], pod["metadata"]["name"])
    limits = {c["name"]: quantity(c["resources"]["limits"]["memory"])
              for c in pod["spec"]["containers"] if "memory" in c.get("resources", {}).get("limits", {})}
    for sp in stats.get("pods", []):
        if (sp["podRef"]["namespace"], sp["podRef"]["name"]) == ref:
            r = [c["memory"]["rssBytes"] / limits[c["name"]] for c in sp.get("containers", [])
                 if c["name"] in limits and "rssBytes" in c.get("memory", {})]
            return round(max(r), 4) if r else None
    return None


def _db(ctx, fc, cache):
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
        ip = p["status"].get("hostIP")
        rss = _rss_ratio(p, _kubelet(cache, ("stats", ip), lambda: ctx.k8s.kubelet_stats(ip)))
        items.append(Item(f"db.{name}", name, Status.OK, {
            "role": role, "cpu_ratio": _cpu_ratio(p, usage), "memory_rss_ratio": rss,
            "restarts_recent": restarts, "oom_recent": oom, "crashloop": crash},
            label("note.metrics_missing") if rss is None else None))
        if p["status"].get("podIP"):
            eps.append(DbEndpoint(name, p["status"]["podIP"], role))
    _db_endpoints = tuple(eps)
    return items


def _cloudflared(ctx, fc, pods, ratios):
    usage = _cpu_usage(ctx, fc.cloudflared.namespace, fc.cloudflared.pod_selector)
    throttle, note = _throttle_max(pods, ratios)
    by_pod = [{"pod": p["metadata"]["name"], "cpu_ratio": _cpu_ratio(p, usage),
               "throttle_ratio": ratios.get(p["metadata"]["name"])} for p in pods]
    return Item("cloudflared", "cloudflared", Status.OK,
                {"pods": len(pods), "cpu_ratio_max": _max(_cpu_ratio(p, usage) for p in pods),
                 "throttle_ratio_max": throttle, "by_pod": by_pod}, note)


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or SOURCE_ID in fc.disabled_sources:
        return make_result(SOURCE_ID, ctx.now(), [], Status.EMPTY)
    cache = {}
    cms_pods, cf_pods = _pods(ctx, fc.cms.namespace, fc.cms.pod_selector), _pods(ctx, fc.cloudflared.namespace, fc.cloudflared.pod_selector)
    ratios = _throttle_ratios(ctx, cache, cms_pods + cf_pods)
    return make_result(SOURCE_ID, ctx.now(), [_cms(ctx, fc, cms_pods, ratios), *_db(ctx, fc, cache),
                                              _cloudflared(ctx, fc, cf_pods, ratios)])


SOURCES = (Source(SOURCE_ID, timedelta(seconds=30), fetch),)
