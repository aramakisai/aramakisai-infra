import model
from model import Status
from render.components import Raw, badge, card, esc, fmt_dt, fmt_int, table
from render.labels import label
from render.sections import Section


def _dt(s):
    return fmt_dt(model.parse_iso(s)) if s else "-"


def _link(url, text) -> Raw:
    # API 由来の値だが、javascript: 等をリンクにしない。
    if not str(url).startswith("https://"):
        return Raw(esc(text))
    return Raw(f'<a href="{esc(url)}" rel="noopener noreferrer">{esc(text)}</a>')


def _tunnel(items):
    i = items[0]
    state = i.values["state"]
    name = label(f"tunnel.{state}") if state in ("healthy", "degraded", "down", "inactive") else state
    return (f'<p>{badge(i.status)} {esc(name)} / {esc(label("tunnel.connections", n=fmt_int(i.values["connections"])))}</p>')


def _tailscale(items):
    rows = []
    for i in sorted(items, key=lambda i: i.label):
        on = bool(i.values["online"])
        rows.append([i.label, Raw(f'{badge(i.status)} {esc(label("online.true" if on else "online.false"))}'),
                     _dt(i.values["last_seen"])])
    return table([label("col.device"), label("col.online"), label("col.last_seen")], rows)


def _ci_part(result, prefix, title, empty, body):
    return card(title, result, body, source="GitHub", empty=empty, select=lambda i: i.key.startswith(prefix))


def _runs(items):
    rows = [[i.label, i.values["repo"], _dt(i.values["failed_at"]), _link(i.values["url"], label("col.link"))]
            for i in sorted(items, key=lambda i: i.values["failed_at"], reverse=True)]
    return table([label("col.workflow"), label("col.repo"), label("col.failed_at"), label("col.link")], rows)


def _prs(items):
    rows = [[_link(i.values["url"], i.label), i.values["repo"], _dt(i.values["opened_at"])] for i in items]
    return table([label("col.pr"), label("col.repo"), label("col.opened")], rows)


def _incidents(items):
    rows = [[Raw(f'{badge(i.status)} {esc(label("incident.dr" if i.values["label"] == "dr-incident" else "incident.infra"))}'),
             _link(i.values["url"], i.label), i.values["repo"], _dt(i.values["opened_at"])] for i in items]
    return table(["", label("col.pr"), label("col.repo"), label("col.opened")], rows)


def render(snapshot, query):
    ci = snapshot.get("ci.github")
    return "".join(str(x) for x in (
        card(label("sub.tunnel"), snapshot.get("connect.tunnel"), _tunnel, source="Cloudflare"),
        card(label("sub.tailscale"), snapshot.get("connect.tailscale"), _tailscale, source="Tailscale"),
        _ci_part(ci, "run.", label("sub.gha"), None, _runs),
        _ci_part(ci, "renovate.", label("sub.renovate"), None, _prs),
        _ci_part(ci, "incident.", label("sub.incidents"), label("incident.none"), _incidents)))


SECTION = Section("connect", "nav.connect", "sec.connect",
                  ("connect.tunnel", "connect.tailscale", "ci.github"), render)
