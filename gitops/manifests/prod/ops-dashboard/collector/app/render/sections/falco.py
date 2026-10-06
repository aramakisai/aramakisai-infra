from render import svg
from render.components import card, esc, fmt_dt, fmt_int, pick_range, range_nav, table
from render.labels import label
from render.sections import Section
import model

OPTIONS = ("24h", "7d", "30d")
PRIORITIES = ("emergency", "alert", "critical", "error", "warning", "notice", "informational", "debug")


def _body(items):
    series = sorted((i for i in items if i.key.startswith("falco.series.")), key=lambda i: i.key)
    rules = [i for i in items if i.key.startswith("falco.rule.")]
    recent = [i.values for i in items if i.key.startswith("falco.recent.")]
    if not rules and not recent:
        return f'<p class="empty">{esc(label("empty.falco"))}</p>'
    chart = svg.bar_chart(label("chart.falco_priority.title"), label("chart.falco_priority.x"),
                          label("chart.falco_priority.y"), [i.label[5:] for i in series],
                          [(label(f"legend.{p}"), [i.values.get(p, 0) for i in series]) for p in PRIORITIES])
    top = svg.hbar_chart(label("chart.falco_rule.title"), label("chart.falco_rule.x"), label("chart.falco_rule.y"),
                         [(i.label, i.values["count"]) for i in rules])
    rows = [[fmt_dt(model.parse_iso(v["time"])) if v["time"][:1].isdigit() else v["time"], v["rule"],
             label(f"legend.{v['priority']}") if v["priority"] in PRIORITIES else v["priority"],
             v["target"], v["summary"]] for v in recent]
    tbl = table([label("col.time"), label("col.rule"), label("col.priority"), label("col.target"), label("col.summary")], rows)
    return f'{chart}{top}<h4>{esc(label("sub.falco_recent"))}</h4>{tbl}'


def render(snapshot, query):
    res = snapshot.get("security.falco")
    notes = [esc(label("note.falco_retention"))]
    failed = next((i for i in (res.items if res else ()) if i.key == "falco.ingest_failed"), None)
    if failed and failed.values["count"]:
        notes.insert(0, esc(label("note.falco_ingest_failed", n=fmt_int(failed.values["count"]))))
    body = card(label("sec.falco"), res, _body, source="Falco")
    notes_html = "".join(f'<p class="note">{n}</p>' for n in notes)
    return f"{range_nav('falco', 'falco', OPTIONS, query)}{notes_html}{body}"


SECTION = Section("falco", "nav.falco", "sec.falco", ("security.falco",), render)
