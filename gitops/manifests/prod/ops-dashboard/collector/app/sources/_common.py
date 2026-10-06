from model import Item, Status
from render.labels import label


def quota_item(ctx, service: str, limit_key: str, used: int) -> Item:
    """宣言プランの上限に対する使用数。プラン未宣言なら上限なしで OK のまま返す。"""
    plan = ctx.config.active_plan(service, ctx.now().date())
    limit = plan.limits.get(limit_key) if plan else None
    status, note = Status.OK, None
    if limit:
        ratio = used / limit
        if ratio >= 1:
            status, note = Status.CRIT, label("note.over_limit")
        elif ratio >= ctx.config.thresholds.warn_ratio:
            status, note = Status.WARN, label("note.threshold", ratio=round(ctx.config.thresholds.warn_ratio * 100))
    return Item("quota", service, status, {"used": used, "limit": limit}, note)


def condition(obj: dict, type_: str) -> dict | None:
    for c in obj.get("status", {}).get("conditions", []):
        if c.get("type") == type_:
            return c
    return None
