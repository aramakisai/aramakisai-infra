"""当日監視の項目の定義と判定。取得層 (sources/festival_*) の値から Judgement を作る。

判定結果は OK・WARN・CRIT・EMPTY (時間外) だけ。取得失敗・取得遅延 (ERROR・STALE) は評価器が上書きする。
"""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Callable, Iterable, Mapping

from config import OPTIONAL_THRESHOLDS, THRESHOLD_DIRECTIONS, FestivalConfig, Threshold
from model import Item, SourceResult, Status, parse_iso


class Category(StrEnum):
    VISITOR = "visitor"
    FUNCTION = "function"
    CAPACITY = "capacity"
    INFRA = "infra"


@dataclass(frozen=True)
class Judgement:
    key: str
    label: str
    status: Status
    value: str
    threshold: str | None
    observed_at: datetime | None
    note: str | None = None
    judged: bool = True  # False は表示のみ (省略できる閾値が未宣言)


@dataclass(frozen=True)
class EvalInput:
    now: datetime
    config: FestivalConfig
    results: Mapping[str, SourceResult]
    falco_counts: Mapping[str, int]
    internal_networks: tuple[object, ...]


@dataclass(frozen=True)
class ItemGroup:
    """同じ規則で 0 個以上の項目を作る定義 (カメラごと・ノードごと等)。"""
    key_prefix: str
    category: Category
    sources: tuple[str, ...]
    link: str | None
    notify: bool
    judge: Callable[[EvalInput], Iterable[Judgement]]


# アカウント ID はこのリポジトリでは秘匿扱いのため、トップの URL にする
CLOUDFLARE_DASHBOARD = "https://dash.cloudflare.com/"
MONITORING_LINK = "/admin/#sec-monitoring"

_SYMBOL = {"above": "≥", "below": "<"}


def grade(name: str, th: Threshold, value: float | None) -> Status:
    """向きごとの等号: above は warn・crit に等しいときも悪い側、below は等しいときは悪くない (「4 未満」)。"""
    if value is None:
        return Status.OK
    if THRESHOLD_DIRECTIONS[name] == "above":
        return Status.CRIT if value >= th.crit else Status.WARN if value >= th.warn else Status.OK
    return Status.CRIT if value < th.crit else Status.WARN if value < th.warn else Status.OK


def threshold_text(name: str, th: Threshold, fmt: Callable[[float], str]) -> str:
    sym = _SYMBOL[THRESHOLD_DIRECTIONS[name]]
    return f"注意 {sym} {fmt(th.warn)} / 異常 {sym} {fmt(th.crit)}"


def judged_value(inp: EvalInput, name: str, key: str, label: str, value: float | None, text: str,
                 fmt: Callable[[float], str], *, observed_at: datetime | None = None, note: str | None = None,
                 skip: bool = False) -> Judgement:
    """閾値名 name で value を判定する。skip は「正常として扱う」(最低要求数未満など)。
    省略できる閾値が未宣言なら判定せず表示のみにする。"""
    th = inp.config.thresholds.get(name)
    if th is None:
        if name not in OPTIONAL_THRESHOLDS:
            raise KeyError(f"閾値が宣言されていません: {name}")
        return Judgement(key, label, Status.OK, text, None, observed_at, note, judged=False)
    status = Status.OK if skip else grade(name, th, value)
    return Judgement(key, label, status, text, threshold_text(name, th, fmt), observed_at, note)


def find_item(inp: EvalInput, source_id: str, key: str) -> Item | None:
    result = inp.results.get(source_id)
    return next((i for i in result.items if i.key == key), None) if result else None


def _pct(v: float) -> str:
    return f"{v * 100:.1f}%"


def _num(v: float) -> str:
    return f"{v:g}"


def _ms(v: float) -> str:
    return f"{v:g} ms"


def _window_end(item: Item) -> datetime | None:
    v = item.values.get("window_end")
    return parse_iso(v) if v else None


def _rate(inp: EvalInput, source: str, key: str, num: str, name: str, jkey: str, label: str, min_key: str):
    item = find_item(inp, source, key)
    if item is None:
        return
    requests, count = item.values["requests"], item.values[num]
    rate = count / requests if requests else 0.0
    yield judged_value(inp, name, jkey, label, rate, f"{_pct(rate)} ({count}/{requests})", _pct,
                       observed_at=_window_end(item), skip=requests < inp.config.min_requests[min_key])


def _metric(source: str, key: str, field: str, name: str, jkey: str, label: str, fmt, unit: str = ""):
    def judge(inp: EvalInput):
        item = find_item(inp, source, key)
        if item is None:
            return
        v = item.values[field]
        yield judged_value(inp, name, jkey, label, v, f"{fmt(v)}{unit}", fmt, observed_at=_window_end(item))
    return judge


def _group(key: str, label: str, sources: tuple[str, ...], judge, link: str | None = CLOUDFLARE_DASHBOARD,
           category: Category = Category.VISITOR) -> ItemGroup:
    return ItemGroup(key, category, sources, link, True, judge)


_CF = ("festival.cloudflare",)


def _worker_error_rate(inp):
    return _rate(inp, "festival.cloudflare", "worker", "errors", "worker_error_rate",
                 "visitor.worker.error_rate", "Worker エラー率", "worker_error_rate")


def _cms_5xx_rate(inp):
    return _rate(inp, "festival.cloudflare", "cms", "status_5xx", "cms_5xx_rate",
                 "visitor.cms.5xx_rate", "CMS 5xx 比率", "cms_5xx_rate")


def _uptime(inp: EvalInput):
    result = inp.results.get("monitor.uptimerobot")
    if result is None:
        return
    # IdP と CMS DB のモニターは基盤の区分の項目にする
    excluded = {inp.config.uptime_monitors["idp"], inp.config.uptime_monitors["cms_db"]}
    for item in result.items:
        if not item.key.startswith("monitor.") or item.label in excluded:
            continue
        status = item.status if item.status in (Status.WARN, Status.CRIT) else Status.OK
        yield Judgement(f"visitor.uptime.{item.key.removeprefix('monitor.')}", item.label, status,
                        str(item.values.get("state")), None, None, item.note)


VISITOR_GROUPS: tuple[ItemGroup, ...] = (
    _group("visitor.worker.error_rate", "Worker エラー率", _CF, _worker_error_rate),
    _group("visitor.worker.exceeded", "Worker リソース超過", _CF,
           _metric("festival.cloudflare", "worker", "exceeded", "worker_exceeded",
                   "visitor.worker.exceeded", "Worker リソース超過", _num, " 件")),
    _group("visitor.worker.cpu_p99", "Worker CPU p99", _CF,
           _metric("festival.cloudflare", "worker", "cpu_p99_ms", "worker_cpu_p99_ms",
                   "visitor.worker.cpu_p99", "Worker CPU p99", _ms)),
    _group("visitor.cms.5xx_rate", "CMS 5xx 比率", _CF, _cms_5xx_rate),
    _group("visitor.cms.origin_p95", "CMS origin 応答 p95", _CF,
           _metric("festival.cloudflare", "cms", "origin_p95_ms", "cms_origin_p95_ms",
                   "visitor.cms.origin_p95", "CMS origin 応答 p95", _ms)),
    _group("visitor.media.4xx", "メディア 4xx", _CF,
           _metric("festival.cloudflare", "media", "status_4xx", "media_4xx",
                   "visitor.media.4xx", "メディア 4xx", _num, " 件")),
    _group("visitor.media.5xx", "メディア 5xx", _CF,
           _metric("festival.cloudflare", "media", "status_5xx", "media_5xx",
                   "visitor.media.5xx", "メディア 5xx", _num, " 件")),
    _group("visitor.uptime.", "UptimeRobot", ("monitor.uptimerobot",), _uptime, MONITORING_LINK),
)

CATALOG: tuple[ItemGroup, ...] = VISITOR_GROUPS
