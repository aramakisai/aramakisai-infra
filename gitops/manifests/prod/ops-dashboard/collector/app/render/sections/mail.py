
from render import svg
from render.components import card, esc, fmt_int, table
from render.labels import label
from render.sections import Section



def _duration(seconds) -> str:
    return "-" if seconds is None else f"{seconds // 3600}:{seconds % 3600 // 60:02d}"


def _queue(items):
    q = next((i for i in items if i.key == "mail.queue"), None)
    if q is None:
        return ""
    v = q.values
    rows = [[label(f"queue.{k}"), fmt_int(v[k])] for k in ("incoming", "active", "deferred", "hold")]
    oldest = (f'<p>{esc(label("queue.oldest", duration=_duration(v["oldest_deferred_age_s"])))}</p>'
              if v["oldest_deferred_age_s"] is not None else "")
    return f'{table(["", ""], rows, num_cols=(1,))}{oldest}'


def _delivery(items):
    series = sorted((i for i in items if i.key.startswith("mail.series.")), key=lambda i: i.key)
    top = [i.values for i in items if i.key.startswith("mail.top.")]
    chart = svg.bar_chart(label("chart.delivery.title"), label("chart.delivery.x"), label("chart.delivery.y"),
                          [i.label[5:] for i in series],
                          [(label("legend.deferred"), [i.values["deferred"] for i in series]),
                           (label("legend.bounced"), [i.values["bounced"] for i in series])])
    return (f'{_queue(items)}{chart}<h4>{esc(label("sub.delivery_top"))}</h4>'
            f'{table([label("col.domain"), label("col.reason"), label("col.count")], [[t["domain"], t["reason"], fmt_int(t["count"])] for t in top], num_cols=(2,))}')


def _tls(items):
    days = sorted((i for i in items if i.key.startswith("tls.day.")), key=lambda i: i.key)
    if not days:
        return None
    return svg.bar_chart(label("chart.tlsrpt.title"), label("chart.tlsrpt.x"), label("chart.tlsrpt.y"),
                         [i.label for i in days],
                         [(label("legend.tls_success"), [i.values["success"] for i in days]),
                          (label("legend.tls_failure"), [i.values["failure"] for i in days])])


def render(snapshot, query):
    # TLS-RPT は mail.reports の items の一部だけを使うので、日別の行がなければ空表示にする。
    tls = card(label("sub.tlsrpt"), snapshot.get("mail.reports"), lambda items: _tls(items) or "", source="mail-agent",
               empty=label("empty.generic"))
    return (card(label("sub.queue"), snapshot.get("mail.delivery"), _delivery, source="mail-agent") + tls)


SECTION = Section("mail", "nav.mail", "sec.mail", ("mail.delivery", "mail.reports"), render)
