"""当日監視の項目の定義と判定。取得層 (sources/festival_*) の値から Judgement を作る。

判定結果は OK・WARN・CRIT・EMPTY (時間外) だけ。取得失敗・取得遅延 (ERROR・STALE) は評価器が上書きする。
"""
import ipaddress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable, Iterable, Mapping

from config import OPTIONAL_THRESHOLDS, THRESHOLD_DIRECTIONS, FestivalConfig, Threshold
from model import Breakdown, FestivalStateRow, Item, MetricUnit, SourceResult, Status, parse_iso, worst
from render.sections.monitoring import NETDATA_URL
from sources.festival_cloudflare import WINDOW
from sources.security_falco import PRIORITIES


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
    alert_on_warn: bool = False  # True は注意で通知する (異常を宣言しない閾値の項目)
    metric: float | None = None  # グラフの値。値を算出できないときは None
    unit: MetricUnit | None = None  # None はグラフの対象外
    warn: float | None = None  # 閾値の線 (metric と同じ単位。期間は秒)
    crit: float | None = None
    breakdown: Breakdown | None = None


@dataclass(frozen=True)
class EvalInput:
    now: datetime
    config: FestivalConfig
    results: Mapping[str, SourceResult]
    falco_counts: Mapping[str, int]
    internal_networks: tuple[object, ...]
    # infra.notifier・infra.heartbeat の入力。評価器が設定の有無と festival_state を渡す
    state: FestivalStateRow | None = None
    notifier_configured: bool = True
    heartbeat_configured: bool = True


@dataclass(frozen=True)
class ItemGroup:
    """同じ規則で 0 個以上の項目を作る定義 (カメラごと・ノードごと等)。"""
    key_prefix: str
    category: Category
    sources: tuple[str, ...]
    link: str | None
    link_label: str | None  # 表記キー (fest.link.*)。link が None なら None
    notify: bool
    judge: Callable[[EvalInput], Iterable[Judgement]]


# アカウント ID はこのリポジトリでは秘匿扱いのため、トップの URL にする
CLOUDFLARE_DASHBOARD = "https://dash.cloudflare.com/"
MONITORING_LINK = "/admin/#sec-monitoring"
CMS_ADMIN = "https://cms.aramakisai.com/admin"
HEALTHCHECKS_CONSOLE = "https://healthchecks.io/"

_SYMBOL = {"above": "≥", "below": "<"}


def grade(name: str, th: Threshold, value: float | None) -> Status:
    """向きごとの等号: above は warn・crit に等しいときも悪い側、below は等しいときは悪くない (「4 未満」)。"""
    if value is None:
        return Status.OK
    if THRESHOLD_DIRECTIONS[name] == "above":
        if th.crit is not None and value >= th.crit:
            return Status.CRIT
        return Status.WARN if value >= th.warn else Status.OK
    if th.crit is not None and value < th.crit:
        return Status.CRIT
    return Status.WARN if value < th.warn else Status.OK


def threshold_text(name: str, th: Threshold, fmt: Callable[[float], str]) -> str:
    sym = _SYMBOL[THRESHOLD_DIRECTIONS[name]]
    if th.crit is None:
        return f"注意 {sym} {fmt(th.warn)}"
    return f"注意 {sym} {fmt(th.warn)} / 異常 {sym} {fmt(th.crit)}"


def _f(v: float | timedelta | None) -> float | None:
    return v.total_seconds() if isinstance(v, timedelta) else v


def with_chart(inp: EvalInput, j: Judgement, name: str, unit: MetricUnit) -> Judgement:
    """グラフの単位と、閾値名 name の線を付ける。閾値が未宣言なら線は None。"""
    th = inp.config.thresholds.get(name)
    return replace(j, unit=unit, warn=_f(th.warn) if th else None, crit=_f(th.crit) if th else None)


def judged_value(inp: EvalInput, name: str, key: str, label: str, value: float | None, text: str,
                 fmt: Callable[[float], str], *, observed_at: datetime | None = None, note: str | None = None,
                 skip: bool = False, mu: MetricUnit | None = None) -> Judgement:
    """閾値名 name で value を判定する。skip は「正常として扱う」(最低要求数未満など)。
    省略できる閾値が未宣言なら判定せず表示のみにする。mu を渡すと value をグラフの値にする。"""
    th = inp.config.thresholds.get(name)
    if th is None:
        if name not in OPTIONAL_THRESHOLDS:
            raise KeyError(f"閾値が宣言されていません: {name}")
        j = Judgement(key, label, Status.OK, text, None, observed_at, note, judged=False)
    else:
        status = Status.OK if skip else grade(name, th, value)
        j = Judgement(key, label, status, text, threshold_text(name, th, fmt), observed_at, note,
                      alert_on_warn=th.crit is None)
    return j if mu is None else replace(with_chart(inp, j, name, mu), metric=_f(value))


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
                       observed_at=_window_end(item), skip=requests < inp.config.min_requests[min_key], mu="ratio")


def _metric(source: str, key: str, field: str, name: str, jkey: str, label: str, fmt, unit: str = "",
            mu: MetricUnit = "count"):
    def judge(inp: EvalInput):
        item = find_item(inp, source, key)
        if item is None:
            return
        v = item.values[field]
        yield judged_value(inp, name, jkey, label, v, f"{fmt(v)}{unit}", fmt, observed_at=_window_end(item), mu=mu)
    return judge


def link_label(link: str | None) -> str | None:
    if link is None:
        return None
    if link.startswith("/admin/#"):
        return "fest.link.admin"
    return {CLOUDFLARE_DASHBOARD: "fest.link.cloudflare", NETDATA_URL: "fest.link.netdata",
            CMS_ADMIN: "fest.link.cms", HEALTHCHECKS_CONSOLE: "fest.link.healthchecks"}[link]


_DASH = "—"


def _bool(v) -> str:
    return _DASH if v is None else "fest.bool.true" if v else "fest.bool.false"


def _p(v) -> str:
    return _DASH if v is None else _pct(v)


def _hms(ts: str | None) -> str:
    return _DASH if not ts else parse_iso(ts).astimezone(_JST).strftime("%H:%M:%S")


def _s(v) -> str:
    return _DASH if v is None else str(v)


def _by_count(inp: EvalInput, key: str, field: str, title: str, col: str, label_col: str):
    item = find_item(inp, "festival.cloudflare", key)
    if item is None or item.values.get(field) is None:
        return None
    rows = sorted(item.values[field], key=lambda r: (-r["count"], str(r[col])))
    return Breakdown(title, (label_col, "fest.col.count"), tuple((str(r[col]), str(r["count"])) for r in rows))


def _bd_worker(inp):
    item = find_item(inp, "festival.cloudflare", "worker")
    if item is None or item.values.get("by_status") is None:
        return None
    rows = sorted(item.values["by_status"], key=lambda r: (-r["requests"], r["status"]))
    return Breakdown("fest.bd.worker_status", ("fest.col.status", "fest.col.requests", "fest.col.errors"),
                     tuple((r["status"], str(r["requests"]), str(r["errors"])) for r in rows))


def _bd_cms(inp):
    return _by_count(inp, "cms", "by_status", "fest.bd.http_status", "status", "fest.col.http_status")


def _bd_media(inp):
    return _by_count(inp, "media", "by_status", "fest.bd.http_status", "status", "fest.col.http_status")


def _bd_cache(inp):
    return _by_count(inp, "cms_cache", "by_cache_status", "fest.bd.cache_status", "cache_status",
                     "fest.col.cache_status")


def _bd_signage(inp):
    item = find_item(inp, "festival.signage", "connections")
    if item is None:
        return None
    return Breakdown("fest.bd.signage_origin", ("fest.col.origin", "fest.col.connections"),
                     tuple((o, str(item.values[o])) for o in ("prod", "dev", "preview", "unknown")))


def _bd_quake(inp):
    result = inp.results.get("festival.quake")
    if result is None:
        return None
    chans = sorted((i for i in result.items if i.key.startswith("channel.")), key=lambda i: i.key)
    if not chans:
        return None
    return Breakdown("fest.bd.quake_channel",
                     ("fest.col.channel", "fest.col.connected", "fest.col.receiving", "fest.col.last_received",
                      "fest.col.test"),
                     tuple((i.key.removeprefix("channel."), _bool(i.values["connected"]), _bool(i.values["receiving"]),
                            _hms(i.values["last_received_at"]), _bool(i.values["test"])) for i in chans))


def _bd_parking(inp):
    cams = sorted(_items(inp, "festival.parking", "camera."), key=lambda i: i.label)
    if not cams:
        return None
    return Breakdown("fest.bd.parking_camera",
                     ("fest.col.camera", "fest.col.lot", "fest.col.captured", "fest.col.judge_succeeded",
                      "fest.col.judge_failures"),
                     tuple((i.label, _s(i.values["lot"]), _hms(i.values["last_captured_at"]),
                            _hms(i.values["judge_last_succeeded_at"]), _s(i.values["judge_consecutive_failures"]))
                           for i in cams))


def _bd_cms_pods(inp):
    item = find_item(inp, "festival.workloads", "cms")
    if item is None or item.values.get("by_pod") is None:
        return None
    pods = sorted(item.values["by_pod"], key=lambda p: p["pod"])
    return Breakdown("fest.bd.pod",
                     ("fest.col.pod", "fest.col.ready", "fest.col.cpu", "fest.col.throttle", "fest.col.restarts"),
                     tuple((p["pod"], _bool(p["ready"]), _p(p["cpu_ratio"]), _p(p["throttle_ratio"]),
                            _restart_text(p)) for p in pods))


def _bd_cloudflared_pods(inp):
    item = find_item(inp, "festival.workloads", "cloudflared")
    if item is None or item.values.get("by_pod") is None:
        return None
    pods = sorted(item.values["by_pod"], key=lambda p: p["pod"])
    return Breakdown("fest.bd.pod", ("fest.col.pod", "fest.col.cpu", "fest.col.throttle"),
                     tuple((p["pod"], _p(p["cpu_ratio"]), _p(p["throttle_ratio"])) for p in pods))


def _bd_db(inp):
    wl = {i.key: i for i in _items(inp, "festival.workloads", "db.")}
    pg = {i.key: i for i in _items(inp, "festival.cnpg", "db.")}
    rows = []
    for key in wl.keys() | pg.keys():
        w, c = wl.get(key), pg.get(key)
        wv, cv = (w.values if w else {}), (c.values if c else {})
        role = wv.get("role") or cv.get("role")
        lag = cv.get("replication_lag_s")
        rows.append((role != "primary", key, (
            key.removeprefix("db."), _s(role), _p(wv.get("cpu_ratio")), _p(wv.get("memory_rss_ratio")),
            _s(cv.get("backends")), _DASH if lag is None else f"{lag:g} 秒",
            _restart_text(wv) if w else _DASH)))
    if not rows:
        return None
    return Breakdown("fest.bd.db_instance",
                     ("fest.col.pod", "fest.col.role", "fest.col.cpu", "fest.col.memory", "fest.col.backends",
                      "fest.col.lag", "fest.col.restarts"), tuple(r for *_, r in sorted(rows)))


def _bd_nodes(inp):
    nodes = sorted(_items(inp, "festival.nodes", "node."), key=lambda i: i.key)
    if not nodes:
        return None
    return Breakdown("fest.bd.node", ("fest.col.node", "fest.col.ready", "fest.col.cpu", "fest.col.memory",
                                      "fest.col.disk"),
                     tuple((i.label, _bool(i.values["ready"]), _p(i.values["cpu_ratio"]), _p(i.values["memory_ratio"]),
                            _p(i.values["disk_ratio"])) for i in nodes))


def _with_breakdown(judge, build):
    """グループの全項目に同じ表を付ける。"""
    def wrapped(inp: EvalInput):
        bd = build(inp)
        return [replace(j, breakdown=bd) for j in judge(inp)]
    return wrapped


def _group(key: str, label: str, sources: tuple[str, ...], judge, link: str | None = CLOUDFLARE_DASHBOARD,
           category: Category = Category.VISITOR, bd=None) -> ItemGroup:
    if bd is not None:
        judge = _with_breakdown(judge, bd)
    return ItemGroup(key, category, sources, link, link_label(link), True, judge)


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


def _worker_requests(inp: EvalInput):
    item = find_item(inp, "festival.cloudflare", "worker")
    if item is not None:
        rps = item.values["requests"] / CF_WINDOW_SECONDS
        yield Judgement("visitor.worker.requests", "Worker 要求数 (秒間)", Status.OK, f"{rps:.2f} req/s", None,
                        _window_end(item), judged=False, metric=rps, unit="rps")


VISITOR_GROUPS: tuple[ItemGroup, ...] = (
    _group("visitor.worker.requests", "Worker 要求数 (秒間)", _CF, _worker_requests),
    _group("visitor.worker.error_rate", "Worker エラー率", _CF, _worker_error_rate, bd=_bd_worker),
    _group("visitor.worker.exceeded", "Worker リソース超過", _CF,
           _metric("festival.cloudflare", "worker", "exceeded", "worker_exceeded",
                   "visitor.worker.exceeded", "Worker リソース超過", _num, " 件"), bd=_bd_worker),
    _group("visitor.worker.cpu_p99", "Worker CPU p99", _CF,
           _metric("festival.cloudflare", "worker", "cpu_p99_ms", "worker_cpu_p99_ms",
                   "visitor.worker.cpu_p99", "Worker CPU p99", _ms, mu="ms")),
    _group("visitor.cms.5xx_rate", "CMS 5xx 比率", _CF, _cms_5xx_rate, bd=_bd_cms),
    _group("visitor.cms.origin_p95", "CMS origin 応答 p95", _CF,
           _metric("festival.cloudflare", "cms", "origin_p95_ms", "cms_origin_p95_ms",
                   "visitor.cms.origin_p95", "CMS origin 応答 p95", _ms, mu="ms")),
    _group("visitor.media.4xx", "メディア 4xx", _CF,
           _metric("festival.cloudflare", "media", "status_4xx", "media_4xx",
                   "visitor.media.4xx", "メディア 4xx", _num, " 件"), bd=_bd_media),
    _group("visitor.media.5xx", "メディア 5xx", _CF,
           _metric("festival.cloudflare", "media", "status_5xx", "media_5xx",
                   "visitor.media.5xx", "メディア 5xx", _num, " 件"), bd=_bd_media),
    _group("visitor.uptime.", "UptimeRobot", ("monitor.uptimerobot",), _uptime, MONITORING_LINK),
)


# --- 機能別 ---

_JST = timezone(timedelta(hours=9))
_TIME_OUT = "時間外"


def _dur(td: timedelta) -> str:
    s = int(td.total_seconds())
    m, sec = divmod(s, 60)
    return f"{m} 分" if not sec else f"{m} 分 {sec} 秒" if m else f"{sec} 秒"


def _hhmm(s: str):
    return datetime.strptime(s, "%H:%M").time()


def _parking_active(inp: EvalInput) -> bool | None:
    """開催日かつ監視時間内 (終了ちょうどを含む) なら True。日程が取れていなければ None。"""
    monitor = find_item(inp, "festival.schedule", "monitor")
    result = inp.results.get("festival.schedule")
    if monitor is None or result is None:
        return None
    local = inp.now.astimezone(_JST)
    on_day = any(parse_iso(i.values["start_at"]).astimezone(_JST).date() == local.date()
                 for i in result.items if i.key.startswith("event_day."))
    return on_day and _hhmm(monitor.values["start"]) <= local.time() <= _hhmm(monitor.values["end"])


def _parking(kind: str):
    def judge(inp: EvalInput):
        cameras = [i for i in (inp.results.get("festival.parking").items if "festival.parking" in inp.results else ())
                   if i.key.startswith("camera.")]
        if not cameras:
            return
        active = _parking_active(inp)
        for cam in cameras:
            key = f"function.parking.{kind}.{cam.key.removeprefix('camera.')}"
            label = f"駐車場 {cam.label} " + ("受信" if kind == "capture" else "判定")
            if active is None:
                j = Judgement(key, label, Status.ERROR, "開催日程未取得", None, None)
            elif not active:
                j = Judgement(key, label, Status.EMPTY, _TIME_OUT, None, None)
            else:
                j = (_capture if kind == "capture" else _judge)(inp, cam, key, label)
            yield with_chart(inp, j, *(("parking_capture_age", "seconds") if kind == "capture"
                                       else ("parking_judge_failures", "count")))
    return judge


def _age(inp: EvalInput, ts: str | None) -> timedelta | None:
    return inp.now - parse_iso(ts) if ts else None


def _capture(inp: EvalInput, cam: Item, key: str, label: str) -> Judgement:
    ts = cam.values["last_captured_at"]
    age = _age(inp, ts)
    if age is None:
        return Judgement(key, label, Status.CRIT, "受信なし", None, None)
    return judged_value(inp, "parking_capture_age", key, label, age, f"最終受信から {_dur(age)}", _dur,
                        observed_at=parse_iso(ts), mu="seconds")


def _judge(inp: EvalInput, cam: Item, key: str, label: str) -> Judgement:
    th_f = inp.config.thresholds.get("parking_judge_failures")
    th_a = inp.config.thresholds.get("parking_judge_success_age")
    fails = cam.values["judge_consecutive_failures"]
    ts = cam.values["judge_last_succeeded_at"]
    age = _age(inp, ts)
    text = f"連続失敗 {fails} 回 / " + ("成功なし" if age is None else f"最終成功から {_dur(age)}")
    at = parse_iso(ts) if ts else None
    err = cam.values.get("judge_last_error") if fails else None
    if th_f is None and th_a is None:
        return Judgement(key, label, Status.OK, text, None, at, err, judged=False, metric=float(fails))
    grades, thr = [], []
    if th_f is not None:
        grades.append(grade("parking_judge_failures", th_f, fails))
        thr.append(f"失敗 {threshold_text('parking_judge_failures', th_f, _num)}")
    if th_a is not None:
        none_status = Status.WARN if th_a.crit is None else Status.CRIT
        grades.append(none_status if age is None else grade("parking_judge_success_age", th_a, age))
        thr.append(f"経過 {threshold_text('parking_judge_success_age', th_a, _dur)}")
    return Judgement(key, label, worst(grades), text, " / ".join(thr), at, err, metric=float(fails),
                     alert_on_warn=any(t.crit is None for t in (th_f, th_a) if t is not None))


def _signage(inp: EvalInput):
    item = find_item(inp, "festival.signage", "connections")
    if item is None:
        return
    prod = item.values["prod"]
    at = parse_iso(item.values["at"]) if item.values.get("at") else None
    yield Judgement("function.signage.prod", "サイネージ接続数 (本番)", Status.OK, f"{prod} 台",
                    None, at, judged=False, metric=prod, unit="count")


def _quake_cron_item(inp: EvalInput) -> Item | None:
    return find_item(inp, "festival.quake", "cron")


def _quake_cron(inp: EvalInput):
    if "festival.quake" not in inp.results:
        return
    key, label = "function.quake.cron", "地震リレー 定期起動"
    cron = _quake_cron_item(inp)
    if cron is None:
        yield Judgement(key, label, Status.ERROR, "定期起動の情報なし", None, None)
    elif not cron.values["in_period"]:
        yield Judgement(key, label, Status.EMPTY, _TIME_OUT, None, None)
    elif cron.values["last_run_at"] is None:
        yield Judgement(key, label, Status.CRIT, "起動なし", None, None)
    else:
        ts = cron.values["last_run_at"]
        age = _age(inp, ts)
        yield judged_value(inp, "quake_cron_age", key, label, age, f"最終実行から {_dur(age)}", _dur,
                           observed_at=parse_iso(ts))


def _quake_channels(inp: EvalInput):
    result = inp.results.get("festival.quake")
    if result is None:
        return
    cron = _quake_cron_item(inp)
    in_period = cron.values["in_period"] if cron else True
    unreachable = find_item(inp, "festival.quake", "unreachable")
    down = bool(unreachable and unreachable.values["value"])
    for item in result.items:
        if not item.key.startswith("channel."):
            continue
        name = item.key.removeprefix("channel.")
        key, label = f"function.quake.channel.{name}", f"地震リレー 上流 {name}"
        ts = item.values["last_received_at"]
        obs = parse_iso(ts) if ts else None
        if not in_period:
            yield Judgement(key, label, Status.EMPTY, _TIME_OUT, None, None)
        elif down:
            yield Judgement(key, label, Status.CRIT, "リレー到達不可", None, obs)
        else:
            ok = item.values["receiving"]
            yield Judgement(key, label, Status.OK if ok else Status.WARN, "受信中" if ok else "受信なし", None, obs)


def _quake_discord(inp: EvalInput):
    item = find_item(inp, "festival.quake", "discord")
    if item is None:
        return
    v = item.values
    key, label = "function.quake.discord", "地震リレー Discord 通知"
    ok_at, ng_at = v["last_success_at"], v["last_failure_at"]
    if v["disabled"]:
        yield Judgement(key, label, Status.CRIT, "無効", None, None, v["last_failure"])
    elif not v["configured"]:
        yield Judgement(key, label, Status.WARN, "未設定", None, None)
    elif ng_at and (not ok_at or parse_iso(ng_at) > parse_iso(ok_at)):
        yield Judgement(key, label, Status.WARN, "最終送信に失敗", None, parse_iso(ng_at), v["last_failure"])
    else:
        yield Judgement(key, label, Status.OK, "正常", None, parse_iso(ok_at) if ok_at else None)


def _stamp(field: str, name: str, suffix: str, label: str):
    return _metric("festival.stamp", "stamp", field, name, f"function.stamp.{suffix}", label, _num, " 件")


def _bus(inp: EvalInput):
    result = inp.results.get("festival.bus")
    if result is None:
        return
    for item in result.items:
        if not item.key.startswith("feed."):
            continue
        feed = item.key.removeprefix("feed.")
        key, label = f"function.bus.{feed}", f"バス {feed}"
        ts = item.values["header_timestamp"]
        if ts is None or item.status is Status.STALE:
            yield with_chart(inp, Judgement(key, label, Status.STALE, "取得失敗", None, None, item.note),
                             "bus_feed_age", "seconds")
            continue
        age = _age(inp, ts)
        yield judged_value(inp, "bus_feed_age", key, label, age, f"生成から {_dur(age)}", _dur,
                           observed_at=parse_iso(ts), mu="seconds")


FUNCTION_GROUPS: tuple[ItemGroup, ...] = (
    _group("function.parking.capture.", "駐車場 受信", ("festival.parking", "festival.schedule"),
           _parking("capture"), CMS_ADMIN, Category.FUNCTION, bd=_bd_parking),
    _group("function.parking.judge.", "駐車場 判定", ("festival.parking", "festival.schedule"),
           _parking("judge"), CMS_ADMIN, Category.FUNCTION, bd=_bd_parking),
    _group("function.signage.prod", "サイネージ接続数 (本番)", ("festival.signage",), _signage,
           CLOUDFLARE_DASHBOARD, Category.FUNCTION, bd=_bd_signage),
    _group("function.quake.cron", "地震リレー 定期起動", ("festival.quake",), _quake_cron, None, Category.FUNCTION),
    _group("function.quake.channel.", "地震リレー 上流", ("festival.quake",), _quake_channels, None,
           Category.FUNCTION, bd=_bd_quake),
    _group("function.quake.discord", "地震リレー Discord 通知", ("festival.quake",), _quake_discord, None,
           Category.FUNCTION),
    _group("function.stamp.failed", "スタンプ 記録失敗", ("festival.stamp",),
           _stamp("failed", "stamp_failed", "failed", "スタンプ 記録失敗"), CLOUDFLARE_DASHBOARD, Category.FUNCTION),
    _group("function.stamp.d1_failed", "スタンプ D1 失敗", ("festival.stamp",),
           _stamp("d1_failed", "stamp_d1_failed", "d1_failed", "スタンプ D1 失敗"), CLOUDFLARE_DASHBOARD,
           Category.FUNCTION),
    _group("function.stamp.rate_limited", "スタンプ レート制限", ("festival.stamp",),
           _stamp("rate_limited", "stamp_rate_limited", "rate_limited", "スタンプ レート制限"), CLOUDFLARE_DASHBOARD,
           Category.FUNCTION),
    _group("function.bus.", "バス フィード", ("festival.bus",), _bus, None, Category.FUNCTION),
)



# --- 容量・基盤・セキュリティ ---

CLUSTER_LINK = "/admin/#sec-cluster"
DATA_LINK = "/admin/#sec-data"
NODE_LINK = "/admin/#sec-node"
CONNECT_LINK = "/admin/#sec-connect"
FALCO_LINK = "/admin/#sec-falco"
FAIL2BAN_LINK = "/admin/#sec-fail2ban"
CF_WINDOW_SECONDS = WINDOW.total_seconds()
_WORKLOADS, _CNPG, _NODES = "festival.workloads", "festival.cnpg", "festival.nodes"
_RANK = {Status.OK: 0, Status.WARN: 1, Status.CRIT: 2}


def _worse(a: Status, b: Status) -> Status:
    return a if _RANK[a] >= _RANK[b] else b


def _items(inp: EvalInput, source: str, prefix: str, role: str | None = None) -> list[Item]:
    r = inp.results.get(source)
    return [] if r is None else [i for i in r.items
                                 if i.key.startswith(prefix) and (role is None or i.values.get("role") == role)]


def _jv(inp, item, name, jkey, label, v, fmt, unit="", text=None, mu=None):
    """v が None (算出不能) のときは「—」で、Item の注記を引き継ぐ (評価器が取得遅延として扱う)。"""
    if text is None:
        text = "—" if v is None else f"{fmt(v)}{unit}"
    return judged_value(inp, name, jkey, label, v, text, fmt, observed_at=_window_end(item),
                        note=item.note if v is None else None, mu=mu)


def _one(source, key, field, name, jkey, label, fmt, unit="", mu="ratio"):
    def judge(inp):
        item = find_item(inp, source, key)
        if item is not None:
            yield _jv(inp, item, name, jkey, label, item.values[field], fmt, unit, mu=mu)
    return judge


def _each(source, prefix, field, name, jkey, label, fmt, role=None, unit="", mu="ratio"):
    """jkey が "." で終われば項目ごとに末尾へ名前を付ける。label は {} に名前を入れる。"""
    def judge(inp):
        for i in _items(inp, source, prefix, role):
            n = i.key.removeprefix(prefix)
            yield _jv(inp, i, name, jkey + n if jkey.endswith(".") else jkey, label.format(n), i.values[field],
                      fmt, unit, mu=mu)
    return judge


def _ratio_of(num, den):
    return None if num is None or not den else num / den


def _cms_ready(inp):
    item = find_item(inp, _WORKLOADS, "cms")
    if item is not None:
        v = item.values
        ratio = v["ready"] / v["replicas"] if v["replicas"] else 0.0
        yield judged_value(inp, "cms_ready_ratio", "capacity.cms.ready", "CMS Ready 率", ratio,
                           f"{v['ready']}/{v['replicas']}", _num)


def _restart_status(v) -> Status:
    return Status.CRIT if v["crashloop"] else Status.OK


def _restart_text(v) -> str:
    parts = (["OOM"] if v["oom_recent"] else []) + (["CrashLoop"] if v["crashloop"] else [])
    if v["restarts_recent"]:
        parts.append(f"再起動 {v['restarts_recent']} 回")
    return " / ".join(parts) or "なし"


def _cms_restarts(inp):
    item = find_item(inp, _WORKLOADS, "cms")
    if item is not None:
        yield Judgement("capacity.cms.restarts", "CMS 再起動", _restart_status(item.values),
                        _restart_text(item.values), None, None)


def _db_restarts(inp):
    pods = _items(inp, _WORKLOADS, "db.")
    if not pods:
        return
    status = Status.OK
    hit = []
    for i in pods:
        status = _worse(status, _restart_status(i.values))
        if _restart_text(i.values) != "なし":
            hit.append(f"{i.key.removeprefix('db.')} ({_restart_text(i.values)})")
    yield Judgement("capacity.db.restarts", "DB 再起動", status, ", ".join(hit) or "なし", None, None)


def _db_connections(inp):
    for i in _items(inp, _CNPG, "db.", "primary"):
        v = _ratio_of(i.values["backends"], i.values["max_connections"])
        text = None if v is None else f"{_pct(v)} ({i.values['backends']}/{i.values['max_connections']:g})"
        yield _jv(inp, i, "db_connections_ratio", "capacity.db.connections", "DB 接続率", v, _pct, text=text, mu="ratio")


def _cms_rps(inp):
    cms, media = find_item(inp, "festival.cloudflare", "cms"), find_item(inp, "festival.cloudflare", "media")
    if cms is not None and media is not None:
        rps = max(cms.values["requests"] - media.values["requests"], 0) / CF_WINDOW_SECONDS
        yield judged_value(inp, "cms_rps", "capacity.cms.rps", "CMS リクエスト (メディア除く)", rps,
                           f"{rps:.2f} req/s", lambda v: f"{v:g}", observed_at=_window_end(cms), mu="rps")


def _cache_hit(inp):
    item = find_item(inp, "festival.cloudflare", "cms_cache")
    if item is not None:
        lookups, hits = item.values["lookups"], item.values["hits"]
        v = hits / lookups if lookups else None
        text = f"{_pct(v)} ({hits}/{lookups})" if v is not None else "照会 0 件"
        yield judged_value(inp, "cms_cache_hit_ratio", "capacity.cms.cache_hit", "Cache API ヒット率", v, text,
                           _pct, observed_at=_window_end(item), mu="ratio")


def _tunnel(inp):
    item = find_item(inp, "connect.tunnel", "tunnel")
    if item is not None:
        n = item.values["connections"]
        j = judged_value(inp, "tunnel_connections", "capacity.tunnel", "Tunnel", n,
                         f"{item.values['state']} / {n} 接続", _num, mu="count")
        yield replace(j, status=_worse(j.status, item.status if item.status in _RANK else Status.OK))


def _node(inp):
    for i in _items(inp, _NODES, "node."):
        n, v = i.key.removeprefix("node."), i.values
        yield Judgement(f"capacity.node.{n}.ready", f"{n} Ready", Status.OK if v["ready"] else Status.CRIT,
                        "Ready" if v["ready"] else "NotReady", None, None)
        for field, name, what in (("cpu_ratio", "node_cpu_ratio", "cpu"), ("memory_ratio", "node_memory_ratio", "memory"),
                                  ("disk_ratio", "node_disk_ratio", "disk")):
            yield _jv(inp, i, name, f"capacity.node.{n}.{what}", f"{n} {what.upper() if what == 'cpu' else what}",
                      v[field], _pct, mu="ratio")
        for field, what, text in (("net_rx_bps", "net_rx", "受信"), ("net_tx_bps", "net_tx", "送信")):
            mbps = None if v[field] is None else v[field] * 8 / 1e6
            yield Judgement(f"capacity.node.{n}.{what}", f"{n} {text}", Status.OK,
                            "—" if mbps is None else f"{mbps:.1f} Mbps", None, None,
                            i.note if mbps is None else None, judged=False, metric=mbps, unit="mbps")


_cap = lambda key, label, sources, judge, link, bd=None: _group(  # noqa: E731
    key, label, sources, judge, link, Category.CAPACITY, bd)
_CMS_RPS = ("festival.cloudflare",)

CAPACITY_GROUPS: tuple[ItemGroup, ...] = (
    _cap("capacity.cms.ready", "CMS Ready 率", (_WORKLOADS,), _cms_ready, CLUSTER_LINK, bd=_bd_cms_pods),
    _cap("capacity.cms.restarts", "CMS 再起動", (_WORKLOADS,), _cms_restarts, CLUSTER_LINK, bd=_bd_cms_pods),
    _cap("capacity.cms.cpu", "CMS CPU", (_WORKLOADS,),
         _one(_WORKLOADS, "cms", "cpu_ratio_max", "pod_cpu_ratio", "capacity.cms.cpu", "CMS CPU 対 limit", _pct),
         NETDATA_URL, bd=_bd_cms_pods),
    _cap("capacity.db.primary_cpu", "DB primary CPU", (_WORKLOADS,),
         _each(_WORKLOADS, "db.", "cpu_ratio", "pod_cpu_ratio", "capacity.db.primary_cpu", "DB primary CPU 対 limit",
               _pct, "primary"), NETDATA_URL, bd=_bd_db),
    _cap("capacity.db.connections", "DB 接続率", (_CNPG,), _db_connections, DATA_LINK, bd=_bd_db),
    _cap("capacity.db.backends.", "DB 接続数", (_CNPG,),
         _each(_CNPG, "db.", "backends", "db_backends", "capacity.db.backends.", "DB {} 接続数", _num, mu="count"), DATA_LINK, bd=_bd_db),
    _cap("capacity.db.memory.", "DB メモリ", (_WORKLOADS,),
         _each(_WORKLOADS, "db.", "memory_rss_ratio", "db_memory_rss_ratio", "capacity.db.memory.",
               "DB {} メモリ (rss) 対 limit", _pct), NETDATA_URL, bd=_bd_db),
    _cap("capacity.db.restarts", "DB 再起動", (_WORKLOADS,), _db_restarts, CLUSTER_LINK, bd=_bd_db),
    _cap("capacity.db.unanalyzed", "DB 統計情報の無い表", (_CNPG,),
         _each(_CNPG, "db.", "unanalyzed_tables", "db_unanalyzed_tables", "capacity.db.unanalyzed",
               "DB 統計情報の無い表", _num, "primary", " 表", "count"), DATA_LINK, bd=_bd_db),
    _cap("capacity.db.lag.", "DB replica 遅延", (_CNPG,),
         _each(_CNPG, "db.", "replication_lag_s", "db_replica_lag_s", "capacity.db.lag.", "DB {} 遅延", _num,
               "replica", " 秒", "seconds"), DATA_LINK, bd=_bd_db),
    _cap("capacity.cms.throttle", "CMS スロットリング", (_WORKLOADS,),
         _one(_WORKLOADS, "cms", "throttle_ratio_max", "cpu_throttle_ratio", "capacity.cms.throttle",
              "CMS CPU スロットリング率", _pct), NETDATA_URL, bd=_bd_cms_pods),
    _cap("capacity.cms.rps", "CMS リクエスト", _CMS_RPS, _cms_rps, CLOUDFLARE_DASHBOARD),
    _cap("capacity.cms.cache_hit", "Cache API ヒット率", _CMS_RPS, _cache_hit, CLOUDFLARE_DASHBOARD, bd=_bd_cache),
    _cap("capacity.cloudflared.cpu", "cloudflared CPU", (_WORKLOADS,),
         _one(_WORKLOADS, "cloudflared", "cpu_ratio_max", "pod_cpu_ratio", "capacity.cloudflared.cpu",
              "cloudflared CPU 対 limit", _pct), NETDATA_URL, bd=_bd_cloudflared_pods),
    _cap("capacity.cloudflared.throttle", "cloudflared スロットリング", (_WORKLOADS,),
         _one(_WORKLOADS, "cloudflared", "throttle_ratio_max", "cpu_throttle_ratio", "capacity.cloudflared.throttle",
              "cloudflared CPU スロットリング率", _pct), NETDATA_URL, bd=_bd_cloudflared_pods),
    _cap("capacity.node.", "ノード", (_NODES,), _node, NODE_LINK, bd=_bd_nodes),
    _cap("capacity.tunnel", "Tunnel", ("connect.tunnel",), _tunnel, CONNECT_LINK),
)


def _incidents(inp):
    r = inp.results.get("ci.github")
    if r is None:
        return
    found = [i for i in r.items if i.key.startswith("incident.")]
    crit = [i for i in found if i.status is Status.CRIT]
    warn = [i for i in found if i.status is Status.WARN]
    text = f"異常 {len(crit)} 件 / 注意 {len(warn)} 件"
    if found:
        text += ": " + " / ".join(i.label for i in crit + warn)
    yield Judgement("infra.incidents", "dr-incident・infra-alert",
                    Status.CRIT if crit else Status.WARN if warn else Status.OK, text, None, None)


def _falco(inp):
    crit_rank = PRIORITIES.index(inp.config.falco.crit_priority)
    warn_rank = PRIORITIES.index(inp.config.falco.warn_priority)
    hits = [(r, p, inp.falco_counts.get(p, 0)) for r, p in enumerate(PRIORITIES) if inp.falco_counts.get(p, 0)]
    status = (Status.CRIT if any(r <= crit_rank for r, _, _ in hits)
              else Status.WARN if any(r <= warn_rank for r, _, _ in hits) else Status.OK)
    fc = inp.config.falco
    yield Judgement("infra.falco", "Falco", status, " / ".join(f"{p} {c} 件" for _, p, c in hits) or "0 件",
                    f"注意 ≥ {fc.warn_priority} / 異常 ≥ {fc.crit_priority}", None)


def _monitor_status(item: Item) -> Status:
    return item.status if item.status in (Status.WARN, Status.CRIT) else Status.OK


def _monitor(name: str, jkey: str, label: str):
    def judge(inp):
        r = inp.results.get("monitor.uptimerobot")
        if r is None:
            return
        item = next((i for i in r.items if i.key.startswith("monitor.") and i.label == inp.config.uptime_monitors[name]),
                    None)
        if item is None:
            yield Judgement(jkey, label, Status.WARN, "モニターが見つかりません", None, None)
        else:
            yield Judgement(jkey, label, _monitor_status(item), str(item.values.get("state")), None, None, item.note)
    return judge


def _healthchecks(inp):
    r = inp.results.get("monitor.healthchecks")
    for i in () if r is None else r.items:
        if i.key.startswith("check."):
            yield Judgement(f"infra.healthchecks.{i.key.removeprefix('check.')}", i.label, _monitor_status(i),
                            str(i.values.get("state")), None, None, i.note)


def _fail2ban(inp):
    ban, nodes = inp.results.get("mail.fail2ban"), inp.results.get(_NODES)
    if ban is None or nodes is None:
        return

    def internal(ip):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(a in n for n in inp.internal_networks)

    n = sum(1 for i in ban.items if i.key.startswith("ban.") and internal(i.values.get("ip")))
    yield Judgement("infra.fail2ban.internal", "内部アドレスの BAN", Status.CRIT if n else Status.OK, f"{n} 件",
                    None, None)


def _failed_after_success(ok_at, failed_at) -> bool:
    return failed_at is not None and (ok_at is None or failed_at > ok_at)


def _notifier(inp):
    if inp.state is None:
        return
    n = inp.state.notifier
    if not inp.notifier_configured:
        status, text = Status.WARN, "未設定"
    elif n.halted:
        status, text = Status.WARN, f"停止中 ({n.last_failure})"
    elif _failed_after_success(n.last_success_at, n.last_failure_at):
        status, text = Status.WARN, f"送信失敗 ({n.last_failure})"
    else:
        status, text = Status.OK, "正常" if n.last_success_at else "未送信"
    yield Judgement("infra.notifier", "通知器", status, text, None, None)


def _heartbeat(inp):
    if inp.state is None:
        return
    s = inp.state
    if not inp.heartbeat_configured:
        status, text = Status.WARN, "未設定"
    elif _failed_after_success(s.heartbeat_ok_at, s.heartbeat_failed_at):
        status, text = Status.WARN, f"送信失敗 ({s.heartbeat_failure})"
    else:
        status, text = Status.OK, "正常" if s.heartbeat_ok_at else "未送信"
    yield Judgement("infra.heartbeat", "生存確認", status, text, None, None)


_MON = "/admin/#sec-monitoring"
_infra = lambda key, label, sources, judge, link, notify=True: ItemGroup(  # noqa: E731
    key, Category.INFRA, sources, link, link_label(link), notify, judge)

INFRA_GROUPS: tuple[ItemGroup, ...] = (
    _infra("infra.incidents", "dr-incident・infra-alert", ("ci.github",), _incidents, CLUSTER_LINK),
    _infra("infra.falco", "Falco", (), _falco, FALCO_LINK),
    _infra("infra.idp", "IdP", ("monitor.uptimerobot",), _monitor("idp", "infra.idp", "IdP 経由の応答"), _MON),
    _infra("infra.cms_db", "CMS DB", ("monitor.uptimerobot",),
           _monitor("cms_db", "infra.cms_db", "CMS DB 経由の応答"), _MON),
    _infra("infra.healthchecks.", "Healthchecks.io", ("monitor.healthchecks",), _healthchecks, _MON),
    _infra("infra.fail2ban.internal", "内部アドレスの BAN", ("mail.fail2ban", _NODES), _fail2ban, FAIL2BAN_LINK),
    _infra("infra.notifier", "通知器", (), _notifier, None, False),
    _infra("infra.heartbeat", "生存確認", (), _heartbeat, HEALTHCHECKS_CONSOLE, False),
)

CATALOG: tuple[ItemGroup, ...] = VISITOR_GROUPS + FUNCTION_GROUPS + CAPACITY_GROUPS + INFRA_GROUPS
