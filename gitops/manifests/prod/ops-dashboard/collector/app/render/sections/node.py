from render.components import Raw, badge, card, esc, fmt_bytes, fmt_int, status_cell, table
from render.labels import label
from render.sections import Section


def _bar(item):
    pct = max(0.0, min(100.0, float(item.values["ratio"])))
    return Raw(f'<svg viewBox="0 0 100 10" width="120" height="12" role="img" aria-label="{esc(fmt_int(pct))}%" '
               f'class="gauge"><rect width="100" height="10" fill="none" stroke="currentColor" opacity=".4"/>'
               f'<rect width="{pct:.1f}" height="10" fill="currentColor"/></svg>')


def _gauges(items):
    rows = []
    for k in ("cpu", "memory", "disk"):
        for i in (i for i in items if i.key.split(":")[0] == k):
            v = i.values
            f = fmt_int if k == "cpu" else fmt_bytes
            rows.append([f'{label(f"gauge.{k}")} ({i.label})', _bar(i),
                         label("gauge.value", ratio=fmt_int(v["ratio"]), used=f(v["used"]), total=f(v["total"])),
                         status_cell(i)])
    return table(["", "", "", label("col.state")], rows, num_cols=(2,))


def _maintenance(items):
    by = {i.key: i for i in items}
    rows = []
    if (k := by.get("k3s")) is not None:
        rows += [[label("col.k3s_running"), k.values["running"]],
                 [label("col.k3s_latest"), Raw(f'{esc(k.values["latest"])} {status_cell(k) if k.note else ""}')]]
    out = ""
    if (o := by.get("os")) is not None:
        v = o.values
        if v:
            sec = v.get("security_fixable_count")
            rows += [[f'{label("col.patches")} ({o.label})', fmt_int(v.get("upgradable_count"))],
                     [f'{label("col.security")} ({o.label})', "-" if sec is None else fmt_int(sec)],
                     [f'{label("col.reboot")} ({o.label})', label("reboot.required" if v.get("reboot_required") else "reboot.not_required")]]
        if o.status.value == "stale":
            out = f'<p>{status_cell(o)}</p>'
    return f'{table(["", ""], rows)}{out}'


def render(snapshot, query):
    return (card(label("sec.node"), snapshot.get("node.resources"), _gauges, source="Kubernetes")
            + card(label("col.patches"), snapshot.get("node.maintenance"), _maintenance, source="K3s / update.k3s.io"))


SECTION = Section("node", "nav.node", "sec.node", ("node.resources", "node.maintenance"), render)
