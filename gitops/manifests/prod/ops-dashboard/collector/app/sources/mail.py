"""mail-agent 経由のメール情報源 (配送・fail2ban・DMARC/TLS-RPT)。解析は collector 側で行う。"""
import re
from datetime import datetime, timedelta, timezone

import http_client
import model
import report_ingest
from model import Item, Source, Status

DEFAULT_URL = "http://mail-agent.prod.svc.cluster.local:8080"
_TOKEN = "OPS_MAIL_AGENT_TOKEN"
_QUEUE_WARN_S = 3600
# 1 回の取得で取り込む上限。DB 消失後の全件再取り込みは次回以降に分割する (timeout を超えないため)。
_MAX_INGEST = 100

_STATUS = re.compile(r"status=(deferred|bounced)\b(?:\s*\((.*)\))?")
_TO = re.compile(r"\bto=<[^>@]*@([^>\s]+)>")
_ADDR = re.compile(r"[^\s<>(),:;]+@[^\s<>(),:;]+")
_SYSLOG_TS = re.compile(r"^([A-Z][a-z]{2})\s+(\d+)\s+(\d\d:\d\d:\d\d)\b")


def _url(ctx):
    return ctx.config.source_settings("mail.delivery").get("url", DEFAULT_URL)


def _get(ctx, path, **kw):
    return ctx.http.get_json(f"{_url(ctx)}{path}", bearer=ctx.secret(_TOKEN), **kw)


def _log_time(line: str, now: datetime) -> datetime | None:
    try:
        t = datetime.fromisoformat(line.split(" ", 1)[0])
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    m = _SYSLOG_TS.match(line)
    if not m:
        return None
    try:
        # 従来形式は年と TZ を持たない。UTC で年を補い、未来になる場合は前年とみなす。
        t = datetime.strptime(f"{now.year} {m[1]} {m[2]} {m[3]}", "%Y %b %d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return t.replace(year=t.year - 1) if t > now + timedelta(days=1) else t


def parse_line(line: str, now: datetime) -> dict | None:
    """宛先ドメインと理由だけを残す。受信者アドレスは理由内のものも含めて保存しない。"""
    s, to, t = _STATUS.search(line), _TO.search(line), _log_time(line, now)
    if not (s and to and t):
        return None
    return {"time": model.iso(t), "status": s[1], "domain": to[1].lower(),
            "reason": _ADDR.sub("<addr>", s[2] or "")[:160]}


def _fetch_delivery(ctx):
    now = ctx.now()
    cur = ctx.store.get_cursor("mail_cursor")
    log = _get(ctx, "/maillog", params={"cursor": cur} if cur else None)
    events = [e for e in (parse_line(l, now) for l in log["lines"]) if e]

    def save(c):
        c.executemany("INSERT INTO mail_events(time,status,recipient_domain,reason) VALUES (?,?,?,?)",
                      [(e["time"], e["status"], e["domain"], e["reason"]) for e in events])
        c.execute("INSERT INTO mail_cursor(id,cursor) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor",
                  (log["cursor"],))
    ctx.store.write(save)

    q = _get(ctx, "/queue")
    oldest = q.get("oldest_deferred_mtime")
    age = max(0, int(now.timestamp() - oldest)) if oldest else None
    warn = q["hold"] > 0 or (age is not None and age > _QUEUE_WARN_S)
    items = [Item("mail.queue", "queue", Status.WARN if warn else Status.OK,
                  {k: q[k] for k in ("incoming", "active", "deferred", "hold")} | {"oldest_deferred_age_s": age})]

    since = model.iso(now - timedelta(days=7))
    counts = {(r[0], r[1]): r[2] for r in ctx.store.query(
        "SELECT substr(time,1,10), status, count(*) FROM mail_events WHERE time >= ? GROUP BY 1, 2", (since,))}
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        items.append(Item(f"mail.series.{day}", day, Status.OK,
                          {"deferred": counts.get((day, "deferred"), 0), "bounced": counts.get((day, "bounced"), 0)}))
    for n, r in enumerate(ctx.store.query(
            "SELECT recipient_domain, reason, count(*) n FROM mail_events WHERE time >= ? "
            "GROUP BY 1, 2 ORDER BY n DESC, 1 LIMIT 10", (since,))):
        items.append(Item(f"mail.top.{n}", r[0], Status.OK,
                          {"domain": r[0], "reason": r[1], "count": r[2]}))
    return model.make_result("mail.delivery", now, items)


def _fetch_fail2ban(ctx):
    now = ctx.now()
    t = now.timestamp()
    items = []
    for b in _get(ctx, "/fail2ban")["bans"]:
        permanent = b["bantime"] < 0
        if not permanent and b["timeofban"] + b["bantime"] <= t:
            continue  # bips は解除済みの履歴も持つ
        until = None if permanent else model.iso(datetime.fromtimestamp(b["timeofban"] + b["bantime"], timezone.utc))
        items.append(Item(f"ban.{b['jail']}.{b['ip']}", b["jail"], Status.OK, {
            "jail": b["jail"], "ip": b["ip"], "bancount": b["bancount"], "until": until,
            "banned_at": model.iso(datetime.fromtimestamp(b["timeofban"], timezone.utc))}))
    return model.make_result("mail.fail2ban", now, items)


def _fetch_reports(ctx):
    now = ctx.now()
    keys = [k["key"] for k in _get(ctx, "/reports")["keys"]]
    done = {r[0] for r in ctx.store.query("SELECT key FROM report_messages")}
    for key in [k for k in keys if k not in done][:_MAX_INGEST]:
        try:
            raw = ctx.http.get_bytes(f"{_url(ctx)}/reports/{key}", bearer=ctx.secret(_TOKEN))
        except http_client.HttpError as e:
            if e.status == 404:  # 一覧取得後に消えた
                continue
            raise
        report_ingest.ingest_message(ctx.store, key, raw, now)
    return model.make_result("mail.reports", now, _report_items(ctx, now))


def _report_items(ctx, now):
    q = ctx.store.query
    d90, d30 = (model.iso(now - timedelta(days=n)) for n in (90, 30))
    items = []
    for r in q("SELECT substr(r.end,1,10) d, sum(CASE WHEN x.dkim='pass' OR x.spf='pass' THEN x.count ELSE 0 END), "
               "sum(CASE WHEN x.dkim='pass' OR x.spf='pass' THEN 0 ELSE x.count END) "
               "FROM dmarc_records x JOIN dmarc_reports r USING (org_name, report_id) WHERE r.end >= ? GROUP BY d", (d90,)):
        items.append(Item(f"dmarc.day.{r[0]}", r[0], Status.OK, {"pass": r[1], "fail": r[2]}))
    for n, r in enumerate(q(
            "SELECT substr(r.end,1,10) d, x.source_ip, r.org_name, x.spf, x.dkim, x.disposition, sum(x.count) "
            "FROM dmarc_records x JOIN dmarc_reports r USING (org_name, report_id) WHERE r.end >= ? "
            "GROUP BY 1,2,3,4,5,6 ORDER BY d DESC LIMIT 1000", (d90,))):
        items.append(Item(f"dmarc.rec.{r[0]}.{n}", r[1], Status.OK, {
            "day": r[0], "source_ip": r[1], "reporter": r[2], "spf": r[3], "dkim": r[4], "disposition": r[5],
            "count": r[6]}))
    for r in q("SELECT substr(end,1,10) d, sum(success), sum(failure) FROM tlsrpt_reports WHERE end >= ? GROUP BY d",
               (d30,)):
        items.append(Item(f"tls.day.{r[0]}", r[0], Status.OK, {"success": r[1], "failure": r[2]}))
    failed = q("SELECT count(*) FROM ingest_failures WHERE source='mail.reports' AND time >= ?", (d30,))[0][0]
    if failed:
        items.append(Item("ingest.failures", "ingest", Status.WARN, {"count": failed}))
    return items


SOURCES = (
    Source("mail.delivery", timedelta(minutes=5), _fetch_delivery),
    Source("mail.reports", timedelta(minutes=30), _fetch_reports, timeout=300),
    Source("mail.fail2ban", timedelta(minutes=5), _fetch_fail2ban),
)
