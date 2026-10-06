import model
from render import svg
from render.components import card, esc, fmt_dt, pick_range, range_nav, table
from render.labels import label
from render.sections import Section

OPTIONS = ("24h", "7d", "30d")
KINDS = ("password", "otp", "otp_sms", "otp_email", "passkey", "locked")


def _body(items, cur):
    unit, n = ("hour", 24) if cur == "24h" else ("day", 7 if cur == "7d" else 30)
    series = sorted((i for i in items if i.key.startswith(f"auth.{unit}.")), key=lambda i: i.key)[-n:]
    chart = svg.bar_chart(label("chart.auth.title"), label("chart.auth.x"), label("chart.auth.y"),
                          [i.label[5:] for i in series],
                          [(label(f"legend.{k}"), [i.values.get(k, 0) for i in series]) for k in KINDS])
    rows = [[fmt_dt(model.parse_iso(i.values["time"])),
             label(f"legend.{i.values['kind']}") if i.values["kind"] in KINDS else i.values["kind"], i.values["user"]]
            for i in items if i.key.startswith("auth.recent.")]
    return (f'{chart}<h4>{esc(label("sub.auth_recent"))}</h4>'
            f'{table([label("col.time"), label("col.event"), label("col.user")], rows)}')


def render(snapshot, query):
    cur = pick_range(query, "auth", OPTIONS)
    body = card(label("sec.auth"), snapshot.get("auth.zitadel"), lambda items: _body(items, cur), source="Zitadel")
    return f"{range_nav('auth', 'auth', OPTIONS, query)}{body}"


SECTION = Section("auth", "nav.auth", "sec.auth", ("auth.zitadel",), render)
