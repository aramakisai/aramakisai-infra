
import model
from render.components import card, fmt_dt, fmt_int, table
from render.labels import label
from render.sections import Section


def _dt(s):
    return fmt_dt(model.parse_iso(s)) if s else label("ban.permanent")


def _body(items):
    by_jail: dict[str, list] = {}
    for i in items:
        by_jail.setdefault(i.values["jail"], []).append(i.values)
    summary = table([label("col.jail"), label("col.banned_count")],
                    [[j, fmt_int(len(v))] for j, v in sorted(by_jail.items())], num_cols=(1,))
    detail = table([label("col.jail"), label("col.ip"), label("col.banned_at"), label("col.ban_until")],
                   [[v["jail"], v["ip"], _dt(v["banned_at"]), _dt(v["until"])]
                    for j in sorted(by_jail) for v in by_jail[j]])
    return f"{summary}{detail}"


def render(snapshot, query):
    return str(card(label("sec.fail2ban"), snapshot.get("mail.fail2ban"), _body, source="mail-agent",
                    empty=label("empty.fail2ban")))


SECTION = Section("fail2ban", "nav.fail2ban", "sec.fail2ban", ("mail.fail2ban",), render)
