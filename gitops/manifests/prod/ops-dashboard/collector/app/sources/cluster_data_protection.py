from datetime import timedelta

from model import Item, Source, Status, make_result, parse_iso
from render.labels import label
from sources._common import condition


def _duration(td: timedelta) -> str:
    h = int(td.total_seconds() // 3600)
    return f"{h // 24} 日" if h >= 48 else f"{h} 時間"


def _cnpg(ctx):
    base = "/apis/postgresql.cnpg.io/v1"
    excl, now = ctx.config.exclude, ctx.now()
    last_ok: dict[tuple[str, str], str] = {}
    for b in ctx.k8s.list_items(f"{base}/backups", excl):
        s = b.get("status", {})
        if s.get("phase") == "completed" and s.get("stoppedAt"):
            k = (b["metadata"]["namespace"], b["spec"]["cluster"]["name"])
            last_ok[k] = max(last_ok.get(k, ""), s["stoppedAt"])  # 同形式の UTC 文字列なので辞書順で足りる
    for c in ctx.k8s.list_items(f"{base}/clusters", excl):
        ns, name = c["metadata"]["namespace"], c["metadata"]["name"]
        ready = (condition(c, "Ready") or {}).get("status") == "True"
        arch = condition(c, "ContinuousArchiving")
        wal_ok = None if arch is None else arch.get("status") == "True"
        backup = last_ok.get((ns, name))
        status, note = Status.OK, None
        max_age = ctx.config.thresholds.backup_max_age.get(f"{ns}/{name}")
        if not ready or wal_ok is False:
            status = Status.CRIT
        if max_age is not None:
            age = now - parse_iso(backup) if backup else None
            if age is None or age > max_age:
                status = Status.CRIT
                note = label("note.backup_old", duration=_duration(age)) if age else "バックアップの成功記録がありません"
        yield Item(f"cnpg/{ns}/{name}", f"{ns}/{name}", status,
                   {"kind": "cnpg", "ready": ready, "phase": c.get("status", {}).get("phase"),
                    "wal_ok": wal_ok, "last_backup": backup}, note)


def _volsync(ctx):
    now, max_age = ctx.now(), ctx.config.thresholds.volsync_max_age
    for r in ctx.k8s.list_items("/apis/volsync.backube/v1alpha1/replicationsources", ctx.config.exclude):
        ns, name = r["metadata"]["namespace"], r["metadata"]["name"]
        s = r.get("status", {})
        last = s.get("lastSyncTime")
        result = s.get("latestMoverStatus", {}).get("result")
        status, note = Status.OK, None
        if result == "Failed":
            status = Status.CRIT
        if last is None or now - parse_iso(last) > max_age:
            status = Status.CRIT
            note = label("note.backup_old", duration=_duration(now - parse_iso(last))) if last else "同期の記録がありません"
        yield Item(f"volsync/{ns}/{name}", f"{ns}/{name}", status,
                   {"kind": "volsync", "last_sync": last, "result": result}, note)


def fetch(ctx):
    return make_result("cluster.data_protection", ctx.now(), [*_cnpg(ctx), *_volsync(ctx)])


SOURCES = (Source("cluster.data_protection", timedelta(minutes=5), fetch),)
