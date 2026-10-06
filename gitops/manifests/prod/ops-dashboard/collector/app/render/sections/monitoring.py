import model
from render.components import Raw, badge, card, esc, fmt_dt, table
from render.labels import LABELS, label
from render.sections import Section

# Netdata Cloud の space は利用者ごとに異なり collector は URL を持たないため、ログイン後の入口へ誘導する。
NETDATA_URL = "https://app.netdata.cloud/"


def _state(prefix, state):
    key = f"{prefix}.{state}"
    return label(key) if key in LABELS else state


def _monitors(items):
    rows = [[i.label, Raw(f'{badge(i.status)} {esc(_state("state", i.values["state"]))}')]
            for i in items if i.key.startswith("monitor.")]
    return table([label("col.monitor"), label("col.state")], rows)


def _checks(items):
    rows = [[i.label, Raw(f'{badge(i.status)} {esc(_state("state.hc", i.values["state"]))}'),
             fmt_dt(model.parse_iso(i.values["last_ping"])) if i.values.get("last_ping") else "-"]
            for i in items if i.key.startswith("check.")]
    return table([label("col.check"), label("col.state"), label("col.last_ping")], rows)


def render(snapshot, query):
    link = f'<p><a href="{esc(NETDATA_URL)}" rel="noopener noreferrer">{esc(label("link.netdata"))}</a></p>'
    return (card(label("sub.uptimerobot"), snapshot.get("monitor.uptimerobot"), _monitors, source="UptimeRobot",
                 select=lambda i: i.key.startswith("monitor."))
            + card(label("sub.healthchecks"), snapshot.get("monitor.healthchecks"), _checks, source="Healthchecks.io",
                   select=lambda i: i.key.startswith("check."))
            + link)


SECTION = Section("monitoring", "nav.monitoring", "sec.monitoring",
                  ("monitor.uptimerobot", "monitor.healthchecks"), render)
