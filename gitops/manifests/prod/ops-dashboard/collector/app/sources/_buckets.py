"""時系列の刻み。表示は JST なので日・時の境界も JST で切る。"""
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
RANGES = {"24h": ("hour", 24), "7d": ("day", 7), "30d": ("day", 30), "90d": ("day", 90)}

# SQLite 側で同じ書式のキーを作る式 (列は UTC の ISO 文字列)。
SQL_HOUR = "strftime('%Y-%m-%dT%H', datetime({col}, '+9 hours'))"
SQL_DAY = "strftime('%Y-%m-%d', datetime({col}, '+9 hours'))"


def pick(query, param: str) -> str:
    v = (query.get(param) or ["24h"])[0]
    return v if v in RANGES else "24h"


def keys(now: datetime, unit: str, n: int) -> list[str]:
    """now を含む最新の刻みで終わる n 個のキー (古い順)。"""
    t = now.astimezone(JST)
    if unit == "hour":
        t = t.replace(minute=0, second=0, microsecond=0)
        return [(t - timedelta(hours=i)).strftime("%Y-%m-%dT%H") for i in range(n - 1, -1, -1)]
    return [(t - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n - 1, -1, -1)]


def sql_expr(unit: str, col: str) -> str:
    return (SQL_HOUR if unit == "hour" else SQL_DAY).format(col=col)


def window_start_utc(now: datetime, unit: str, n: int) -> datetime:
    t = now.astimezone(JST)
    t = t.replace(minute=0, second=0, microsecond=0) if unit == "hour" else t.replace(hour=0, minute=0, second=0, microsecond=0)
    return (t - (timedelta(hours=n - 1) if unit == "hour" else timedelta(days=n - 1))).astimezone(timezone.utc)
