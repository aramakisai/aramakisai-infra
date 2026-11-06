from datetime import timedelta

from model import Item, Source, Status, make_result
from render.labels import label
from sources import _promtext, festival_workloads

SOURCE_ID = "festival.cnpg"
_NAMES = ("cnpg_backends_total", "cnpg_pg_settings_setting", "cnpg_pg_replication_lag",
          "cnpg_festival_unanalyzed_tables_count")
_FIELDS = ("backends", "max_connections", "replication_lag_s", "unanalyzed_tables")


def _first(samples):
    return samples[0][1] if samples else None


def _values(text: str, role: str) -> dict:
    s = _promtext.parse_samples(text, _NAMES)
    backends = s["cnpg_backends_total"]
    return {"role": role,
            "backends": int(sum(v for _, v in backends)) if backends else None,
            "max_connections": _first([x for x in s["cnpg_pg_settings_setting"] if x[0].get("name") == "max_connections"]),
            "replication_lag_s": _first(s["cnpg_pg_replication_lag"]) if role != "primary" else None,
            "unanalyzed_tables": _first(s["cnpg_festival_unanalyzed_tables_count"]) if role == "primary" else None}


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or SOURCE_ID in fc.disabled_sources:
        return make_result(SOURCE_ID, ctx.now(), [], Status.EMPTY)
    endpoints = festival_workloads.db_endpoints()
    if not endpoints:
        raise RuntimeError("DB の Pod がまだ取得されていません")
    items, last_err = [], None
    for e in endpoints:
        try:
            text = ctx.http.get_bytes(f"http://{e.ip}:9187/metrics", timeout=5).decode("utf-8", "replace")
        except Exception as err:  # Pod IP を含み得る例外文は保存しない
            last_err = err
            # catalog が values[field] を直接読むので、正常時と同じキーを None で持たせる
            items.append(Item(f"db.{e.pod}", e.pod, Status.STALE, {**dict.fromkeys(_FIELDS), "role": e.role},
                              label("note.metrics_missing")))
            continue
        items.append(Item(f"db.{e.pod}", e.pod, Status.OK, _values(text, e.role)))
    if all(i.status == Status.STALE for i in items):
        raise last_err
    return make_result(SOURCE_ID, ctx.now(), items)


SOURCES = (Source(SOURCE_ID, timedelta(seconds=30), fetch),)
