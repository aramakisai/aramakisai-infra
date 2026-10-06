import model
from render.components import Raw, card, esc, fmt_dt, status_cell, table
from render.labels import label
from render.sections import Section


def _dt(s):
    return fmt_dt(model.parse_iso(s)) if s else "-"


def _cnpg(items):
    rows = []
    for i in items:
        v = i.values
        if v.get("kind") != "cnpg":
            continue
        wal = "-" if v["wal_ok"] is None else label("wal.ok" if v["wal_ok"] else "wal.failing")
        rows.append([i.label, v["phase"] or "-", wal, _dt(v["last_backup"]), status_cell(i)])
    return table([label("col.db"), label("col.db_health"), label("col.wal"), label("col.last_backup"),
                  label("col.state")], rows)


_RESULT = {"Successful": "result.successful", "Failed": "result.failed"}


def _volsync(items):
    rows = [[i.label, _dt(i.values["last_sync"]),
             label(_RESULT[i.values["result"]]) if i.values["result"] in _RESULT else (i.values["result"] or "-"),
             status_cell(i)] for i in items if i.values.get("kind") == "volsync"]
    return table([label("col.volsync_target"), label("col.last_sync"), label("col.result"), label("col.state")], rows)


def render(snapshot, query):
    r = snapshot.get("cluster.data_protection")
    return (card(label("sub.cnpg"), r, _cnpg, source="Kubernetes", select=lambda i: i.values.get("kind") == "cnpg")
            + card(label("sub.volsync"), r, _volsync, source="Kubernetes",
                   select=lambda i: i.values.get("kind") == "volsync"))


SECTION = Section("data", "nav.data", "sec.data", ("cluster.data_protection",), render)
