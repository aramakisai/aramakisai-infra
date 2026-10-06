import model
from render.components import Raw, card, esc, fmt_dt, fmt_int, status_cell, table
from render.labels import LABELS, label
from render.sections import Section


def _txt(prefix, v):
    key = f"{prefix}.{str(v).lower()}"
    return label(key) if key in LABELS else str(v)


def _kind(items, kind):
    return [i for i in items if i.values.get("kind") == kind]


def _dt(s):
    return fmt_dt(model.parse_iso(s)) if s else "-"


def _apps(items):
    return table([label("col.app"), label("col.sync"), label("col.health"), label("col.state")],
                 [[i.label, _txt("sync", i.values["sync"]), _txt("health", i.values["health"]), status_cell(i)]
                  for i in _kind(items, "argocd")])


_REASON = {"CrashLoopBackOff": "reason.crashloop", "OOMKilled": "reason.oom"}


def _pods(items):
    return table([label("col.pod"), label("col.restarts"), label("col.reason"), label("col.state")],
                 [[i.label, fmt_int(i.values["restarts"]),
                   label(_REASON[i.values["reason"]]) if i.values["reason"] in _REASON else "-", status_cell(i)]
                  for i in _kind(items, "pod")], num_cols=(1,))


def _certs(items):
    return table([label("col.cert"), label("col.expires"), label("col.days_left"), label("col.state")],
                 [[i.label, _dt(i.values["expires"]), fmt_int(i.values["days_left"]), status_cell(i)]
                  for i in sorted(_kind(items, "cert"), key=lambda i: (i.values["days_left"] is None, i.values["days_left"]))],
                 num_cols=(2,))


def _ready(items, kind, head):
    return table([head, label("col.ready"), label("col.synced_at")],
                 [[i.label, Raw(f'{status_cell(i)} {esc(label("ready.true" if i.values["ready"] else "ready.false"))}'),
                   _dt(i.values["synced_at"])] for i in _kind(items, kind)])


def render(snapshot, query):
    r = snapshot.get("cluster.workloads")
    src = "Kubernetes"
    def of(kind):
        return lambda i: i.values.get("kind") == kind
    return "".join([
        card(label("sub.argocd"), r, _apps, source=src, select=of("argocd")),
        card(label("sub.pods"), r, _pods, source=src, select=of("pod")),
        card(label("sub.certs"), r, _certs, source=src, select=of("cert")),
        card(label("sub.eso"), r, lambda i: _ready(i, "eso", label("col.secret")), source=src, select=of("eso")),
        card(label("sub.store"), r, lambda i: _ready(i, "store", label("sub.store")), source=src, select=of("store")),
    ])


SECTION = Section("cluster", "nav.cluster", "sec.cluster", ("cluster.workloads",), render)
