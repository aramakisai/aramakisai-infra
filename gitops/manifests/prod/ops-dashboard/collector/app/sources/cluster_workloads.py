from datetime import timedelta

from model import Item, Source, Status, make_result, parse_iso
from render.labels import label
from sources._common import condition

OOM_RECENT = timedelta(hours=24)


def _apps(ctx):
    excl = ctx.config.exclude
    for a in ctx.k8s.list_items("/apis/argoproj.io/v1alpha1/applications", excl):
        # 凍結中ワークロードの Application は名前でなく配備先 Namespace で宣言されることもある。
        if a.get("spec", {}).get("destination", {}).get("namespace") in excl:
            continue
        sync = a.get("status", {}).get("sync", {}).get("status", "Unknown")
        health = a.get("status", {}).get("health", {}).get("status", "Unknown")
        if health in ("Degraded", "Missing"):
            st = Status.CRIT
        elif sync != "Synced" or health in ("Suspended", "Unknown"):
            st = Status.WARN
        else:
            st = Status.OK
        name = a["metadata"]["name"]
        yield Item(f"argocd/{name}", name, st, {"kind": "argocd", "sync": sync, "health": health})


def _pods(ctx):
    th, now = ctx.config.thresholds, ctx.now()
    for p in ctx.k8s.list_items("/api/v1/pods", ctx.config.exclude):
        if p.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        restarts, reason = 0, None
        for cs in p.get("status", {}).get("containerStatuses", []):
            restarts += cs.get("restartCount", 0)
            if cs.get("state", {}).get("waiting", {}).get("reason") == "CrashLoopBackOff":
                reason = "CrashLoopBackOff"
            term = cs.get("state", {}).get("terminated") or cs.get("lastState", {}).get("terminated") or {}
            # lastState は次の再起動まで残るため、直近のものだけを異常として拾う。
            if (reason is None and term.get("reason") == "OOMKilled" and term.get("finishedAt")
                    and now - parse_iso(term["finishedAt"]) < OOM_RECENT):
                reason = "OOMKilled"
        if reason is None and restarts < th.pod_restart_warn:
            continue
        ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
        yield Item(f"pod/{ns}/{name}", f"{ns}/{name}", Status.CRIT if reason == "CrashLoopBackOff" else Status.WARN,
                   {"kind": "pod", "restarts": restarts, "reason": reason})


def _certs(ctx):
    th, now = ctx.config.thresholds, ctx.now()
    for c in ctx.k8s.list_items("/apis/cert-manager.io/v1/certificates", ctx.config.exclude):
        ns, name = c["metadata"]["namespace"], c["metadata"]["name"]
        not_after = c.get("status", {}).get("notAfter")
        ready = (condition(c, "Ready") or {}).get("status") == "True"
        if not_after is None:
            yield Item(f"cert/{ns}/{name}", f"{ns}/{name}", Status.CRIT if not ready else Status.WARN,
                       {"kind": "cert", "expires": None, "days_left": None})
            continue
        days = (parse_iso(not_after) - now).days
        st = Status.CRIT if days <= th.cert_crit_days or not ready else (
            Status.WARN if days <= th.cert_warn_days else Status.OK)
        yield Item(f"cert/{ns}/{name}", f"{ns}/{name}", st, {"kind": "cert", "expires": not_after, "days_left": days},
                   label("note.cert_days", n=days))


def _ready_items(ctx, path, kind, key):
    for o in ctx.k8s.list_items(path, ctx.config.exclude):
        ns = o["metadata"].get("namespace")
        name = o["metadata"]["name"]
        ready = (condition(o, "Ready") or {}).get("status") == "True"
        yield Item(f"{kind}/{ns}/{name}" if ns else f"{kind}/{name}", f"{ns}/{name}" if ns else name,
                   Status.OK if ready else Status.CRIT,
                   {"kind": kind, "ready": ready, "synced_at": o.get("status", {}).get(key)})


def fetch(ctx):
    items = [*_apps(ctx), *_pods(ctx), *_certs(ctx),
             *_ready_items(ctx, "/apis/external-secrets.io/v1/externalsecrets", "eso", "refreshTime"),
             *_ready_items(ctx, "/apis/external-secrets.io/v1/clustersecretstores", "store", "refreshTime")]
    return make_result("cluster.workloads", ctx.now(), items)


SOURCES = (Source("cluster.workloads", timedelta(minutes=2), fetch),)
