import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping, Sequence


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    CRIT = "crit"
    ERROR = "error"
    STALE = "stale"
    EMPTY = "empty"


# 集約時の重さ。取得失敗 (ERROR) は OK・STALE・WARN のどれにも埋もれさせない。
_SEVERITY = {Status.EMPTY: 0, Status.OK: 1, Status.STALE: 2, Status.WARN: 3, Status.ERROR: 4, Status.CRIT: 5}


def worst(statuses) -> Status:
    statuses = list(statuses)
    return max(statuses, key=_SEVERITY.__getitem__) if statuses else Status.EMPTY


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    status: Status
    values: Mapping[str, str | int | float | None]
    note: str | None = None


@dataclass(frozen=True)
class SourceResult:
    source_id: str
    status: Status
    fetched_at: datetime
    last_success_at: datetime | None
    items: Sequence[Item]
    error: str | None = None


@dataclass(frozen=True)
class Source:
    source_id: str
    # None は描画時に評価する情報源 (render を使う)。スケジュール取得はしない。
    interval: timedelta | None
    fetch: Callable[["FetchContext"], SourceResult] | None
    render: Callable[["FetchContext", Mapping[str, list[str]]], SourceResult] | None = None
    timeout: float | None = None


class MissingSecret(Exception):
    pass


@dataclass(frozen=True)
class FetchContext:
    config: Any
    store: Any
    http: Any
    k8s: Any
    env: Mapping[str, str] = field(default_factory=dict)
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self.clock()

    def secret(self, name: str) -> str:
        v = self.env.get(name, "")
        path = self.env.get(name + "_FILE")
        if not v and path:
            # 定期再発行される Secret は env に載せると更新されないため、取得のたびにファイルから読む
            try:
                with open(path) as f:
                    v = f.read().strip()
            except OSError:
                v = ""
        if not v:
            raise MissingSecret(f"{name} is not set")
        return v


def make_result(source_id: str, now: datetime, items: Sequence[Item], status: Status | None = None,
                error: str | None = None) -> SourceResult:
    """成功結果を作る。status 省略時は items の最も重い状態 (items なしは EMPTY)。"""
    items = tuple(items)
    return SourceResult(source_id, status or worst(i.status for i in items), now, now, items, error)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


_QUERY = re.compile(r"\?[^\s\"')]*")


def safe_error(exc: BaseException) -> str:
    """URL のクエリ文字列 (API キーが載り得る) を落とした 1 行の理由。"""
    return _QUERY.sub("?…", f"{type(exc).__name__}: {exc}")[:300]


def result_to_json(r: SourceResult) -> str:
    return json.dumps({
        "source_id": r.source_id, "status": r.status.value, "fetched_at": iso(r.fetched_at),
        "last_success_at": iso(r.last_success_at) if r.last_success_at else None,
        "items": [{"key": i.key, "label": i.label, "status": i.status.value, "values": dict(i.values), "note": i.note}
                  for i in r.items],
        "error": r.error,
    }, ensure_ascii=False)


def result_from_json(s: str) -> SourceResult:
    d = json.loads(s)
    return SourceResult(
        d["source_id"], Status(d["status"]), parse_iso(d["fetched_at"]),
        parse_iso(d["last_success_at"]) if d["last_success_at"] else None,
        tuple(Item(i["key"], i["label"], Status(i["status"]), i["values"], i["note"]) for i in d["items"]),
        d["error"])
