from datetime import datetime, timedelta, timezone

from render import svg
from render.components import card, esc, fmt_int, pick_range, range_nav, table
from render.labels import label
from render.sections import Section

OPTIONS = ("7d", "30d", "90d")
_DAYS = {"7d": 7, "30d": 30, "90d": 90}


def _body(items, days, today):
    cats = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    daily = {i.label: i.values for i in items if i.key.startswith("dmarc.day.")}
    recs = [i.values for i in items if i.key.startswith("dmarc.rec.") and i.values["day"] in cats]
    chart = svg.bar_chart(label("chart.dmarc.title"), label("chart.dmarc.x"), label("chart.dmarc.y"), cats,
                          [(label("legend.dmarc_pass"), [daily.get(c, {}).get("pass", 0) for c in cats]),
                           (label("legend.dmarc_fail"), [daily.get(c, {}).get("fail", 0) for c in cats])])
    agg: dict[tuple, int] = {}
    for r in recs:
        k = (r["source_ip"], r["reporter"], r["spf"], r["dkim"], r["disposition"])
        agg[k] = agg.get(k, 0) + r["count"]
    rows = [[ip, rep, label("eval.pass" if spf == "pass" else "eval.fail"),
             label("eval.pass" if dkim == "pass" else "eval.fail"),
             label(f"disposition.{disp}") if disp in ("none", "quarantine", "reject") else disp, fmt_int(n)]
            for (ip, rep, spf, dkim, disp), n in sorted(agg.items(), key=lambda kv: -kv[1])]
    tbl = table([label("col.source_ip"), label("col.reporter"), label("col.spf"), label("col.dkim"),
                 label("col.disposition"), label("col.count")], rows, num_cols=(5,))
    return f'{chart}<h4>{esc(label("sub.dmarc_sources"))}</h4>{tbl}'


def render(snapshot, query):
    cur = pick_range(query, "dmarc", OPTIONS)
    res = snapshot.get("mail.reports")
    today = (res.fetched_at if res else datetime.now(timezone.utc)).astimezone(timezone.utc)
    failed = next((i for i in (res.items if res else ()) if i.key == "ingest.failures"), None)
    note = f'<p class="note">{esc(label("note.ingest_failed", n=fmt_int(failed.values["count"])))}</p>' if failed else ""
    has_dmarc = res and any(i.key.startswith("dmarc.day.") for i in res.items)
    body = card(label("sec.dmarc"), res, lambda items: _body(items, _DAYS[cur], today) if has_dmarc else
                f'<p class="empty">{esc(label("empty.generic"))}</p>', source="mail-agent")
    return f"{range_nav('dmarc', 'dmarc', OPTIONS, query)}{note}{body}"


SECTION = Section("dmarc", "nav.dmarc", "sec.dmarc", ("mail.reports",), render)
