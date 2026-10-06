"""PlanEvaluator: 宣言 (dashboard.toml) と取得した使用量・実プランから Item と状態を作る。"""
from datetime import date, datetime, timedelta, timezone
from typing import Mapping

from model import Item, Status
from render.labels import label

JST = timezone(timedelta(hours=9))


def jst_today(now: datetime) -> date:
    return now.astimezone(JST).date()


def plan_item(cfg, service: str, now: datetime, actual_paid: bool | None = None) -> Item:
    """宣言上の有効プランと戻し忘れの判定。actual_paid は API で取れた実プランが有料か (不明は None)。"""
    today = jst_today(now)
    plan = cfg.active_plan(service, today)
    key = f"plan.{service}"
    if plan is None:
        return Item(key, service, Status.WARN, {"service": service, "plan": None, "paid": None,
                                                "from": None, "until": None}, label("note.plan_undeclared"))
    past = [p for p in cfg.plans if p.service == service and p.paid and p.until is not None and p.until < today]
    status, note = Status.OK, None
    if past:
        last = max(past, key=lambda p: p.until)
        contiguous_paid = plan.paid and plan.from_ <= last.until + timedelta(days=1)
        if contiguous_paid or (not plan.paid and actual_paid):
            status, note = Status.CRIT, label("note.plan_revert", until=last.until.isoformat())
    return Item(key, service, status, {"service": service, "plan": plan.name, "paid": int(plan.paid),
                                       "from": plan.from_.isoformat(),
                                       "until": plan.until.isoformat() if plan.until else None}, note)


def usage_status(used: float | None, limit: float | None, warn_ratio: float) -> tuple[Status, str | None, float | None]:
    if used is None or not limit:
        return Status.OK, None, None
    ratio = used / limit
    if ratio >= 1.0:
        return Status.CRIT, label("note.over_limit"), ratio
    if ratio >= warn_ratio:
        return Status.WARN, label("note.threshold", ratio=round(warn_ratio * 100)), ratio
    return Status.OK, None, ratio


def quota_item(key: str, label_key: str, used: float | None, limit: float | None, unit: str,
               warn_ratio: float, note: str | None = None) -> Item:
    status, auto_note, ratio = usage_status(used, limit, warn_ratio)
    return Item(key, label(label_key), status,
                {"used": used, "limit": limit, "ratio": ratio, "unit": unit}, note or auto_note)


def credential_items(cfg, now: datetime) -> list[Item]:
    today = jst_today(now)
    out = []
    for c in cfg.credentials:
        days = (c.expires_on - today).days
        if days < 0:
            status, note = Status.CRIT, label("note.credential_expired")
        elif days <= 30:
            status, note = Status.WARN, label("note.credential_expiring", n=days)
        else:
            status, note = Status.OK, None
        out.append(Item(f"cred.{c.name}", c.name, status,
                        {"expires_on": c.expires_on.isoformat(), "days": days}, note))
    return out


def hetzner_estimate(servers: list[Mapping], prices: Mapping[tuple[str, str], Mapping], now: datetime,
                     os_base_fee: float = 0.0) -> dict:
    """servers: Hetzner /servers の要素。prices: (server_type, location) -> /pricing の prices 要素。
    費用はすべて税抜。稼働時間は当月 (UTC) 分で、時間課金の累計が月額を超えたら月額で頭打ち。"""
    month_start = now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total, per_server = os_base_fee, {}
    for s in servers:
        p = prices.get((s["server_type"]["name"], s["location"]["name"]))
        if p is None:
            per_server[s["name"]] = None
            continue
        created = datetime.fromisoformat(s["created"].replace("Z", "+00:00"))
        hours = max(0.0, (now - max(created, month_start)).total_seconds() / 3600)
        compute = min(float(p["price_hourly"]["net"]) * hours, float(p["price_monthly"]["net"]))
        over = max(0, (s.get("outgoing_traffic") or 0) - (s.get("included_traffic") or 0))
        traffic = over / 1e12 * float(p["price_per_tb_traffic"]["net"])
        per_server[s["name"]] = round(compute + traffic, 4)
        total += compute + traffic
    return {"total": round(total, 2), "per_server": per_server}


def server_diff(cfg, servers: list[Mapping], now: datetime) -> tuple[list[str], list[str]]:
    """(宣言にないのに存在するサーバー名, 期待されるのに running でないサーバー名)。"""
    today = jst_today(now)
    expected = {s.name for s in cfg.servers if s.until is None or today <= s.until}
    by_name = {s["name"]: s for s in servers}
    unexpected = sorted(set(by_name) - expected)
    missing = sorted(n for n in expected if n not in by_name or by_name[n].get("status") != "running")
    return unexpected, missing
