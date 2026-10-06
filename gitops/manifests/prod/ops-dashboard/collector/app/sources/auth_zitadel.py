from datetime import timedelta

import model
from model import Item, Source, Status
from sources import _buckets

DEFAULT_URL = "http://zitadel.zitadel.svc.cluster.local:8080"
PAGE = 1000
MAX_PAGES = 20
RECENT_LIMIT = 50

TYPES = {
    "user.human.password.check.failed": "password",
    "user.human.mfa.otp.check.failed": "otp",
    "user.human.otp.sms.check.failed": "otp_sms",
    "user.human.otp.email.check.failed": "otp_email",
    "user.human.passwordless.token.check.failed": "passkey",
    "user.locked": "locked",
}


def _ingest(ctx):
    s = ctx.config.source_settings("auth.zitadel")
    headers = {"x-zitadel-instance-host": s["instance_host"]} if s.get("instance_host") else None
    url = f"{s.get('base_url', DEFAULT_URL)}/admin/v1/events/_search"
    token = ctx.secret("OPS_ZITADEL_READER_PAT")
    cursor = ctx.store.get_cursor("auth_cursor")
    for _ in range(MAX_PAGES):
        # sequence は集約ごとの採番でグローバルな順序ではないため、asc + from (creationDate) で古い順に進む。
        # from は境界を含むので、境界の重複は (user_id, sequence) の主キーで吸収する。
        body = {"asc": True, "limit": PAGE, "eventTypes": list(TYPES)}
        if cursor:
            body["from"] = cursor
        events = ctx.http.post_json(url, body, headers=headers, bearer=token).get("events") or []
        if not events:
            return
        rows = [(int(e["sequence"]), e["creationDate"], e["type"]["type"], (e.get("aggregate") or {}).get("id") or "",
                 (e.get("payload") or {}).get("loginName")) for e in events]
        ctx.store.write(lambda c: c.executemany(
            "INSERT OR IGNORE INTO auth_events(sequence,created_at,event_type,user_id,login_name) VALUES (?,?,?,?,?)",
            rows))
        last = events[-1]["creationDate"]
        ctx.store.set_cursor("auth_cursor", last)
        if len(events) < PAGE or last == cursor:
            return
        cursor = last


def _series(ctx, unit, n):
    now = ctx.now()
    start = model.iso(_buckets.window_start_utc(now, unit, n))
    counts = {k: dict.fromkeys(TYPES.values(), 0) for k in _buckets.keys(now, unit, n)}
    for r in ctx.store.query(
            f"SELECT {_buckets.sql_expr(unit, 'created_at')} AS b, event_type AS t, count(*) AS c FROM auth_events "
            "WHERE created_at >= ? GROUP BY b, t", (start,)):
        if r["b"] in counts and r["t"] in TYPES:
            counts[r["b"]][TYPES[r["t"]]] += r["c"]
    return [Item(f"auth.{unit}.{k}", k, Status.OK, v) for k, v in counts.items()]


def fetch(ctx):
    _ingest(ctx)
    items = _series(ctx, "hour", 24) + _series(ctx, "day", 90)
    for r in ctx.store.query(
            "SELECT sequence, created_at, event_type, user_id, login_name FROM auth_events "
            "ORDER BY created_at DESC, sequence DESC LIMIT ?", (RECENT_LIMIT,)):
        kind = TYPES.get(r["event_type"], r["event_type"])
        items.append(Item(f"auth.recent.{r['user_id']}.{r['sequence']}", kind, Status.OK, {
            "time": r["created_at"], "kind": kind, "user": r["login_name"] or r["user_id"]}))
    return model.make_result("auth.zitadel", ctx.now(), items)


SOURCES = (Source("auth.zitadel", timedelta(minutes=5), fetch),)
