import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, Mapping

from sources.security_falco import PRIORITIES as FALCO_PRIORITIES

KNOWN_SERVICES = (
    "cloudflare_workers", "cloudflare_r2", "cloudflare_zero_trust", "hcp_terraform", "infisical",
    "tailscale", "netdata_cloud", "healthchecks", "uptimerobot", "github", "hetzner_object_storage",
)

KNOWN_SOURCE_IDS = (
    "billing.hetzner", "billing.hetzner_os", "billing.cloudflare", "billing.github", "billing.hcp_terraform",
    "billing.infisical", "billing.tailscale", "billing.netdata", "monitor.uptimerobot", "monitor.healthchecks",
    "node.resources", "node.maintenance", "cluster.workloads", "cluster.data_protection", "connect.tunnel",
    "connect.tailscale", "ci.github", "mail.delivery", "mail.reports", "mail.fail2ban", "auth.zitadel",
    "security.falco",
)

# 当日監視の情報源。KNOWN_SOURCE_IDS (既存画面の情報源) には混ぜない
FESTIVAL_SOURCE_IDS = (
    "festival.cloudflare", "festival.stamp", "festival.quake", "festival.signage", "festival.parking",
    "festival.schedule", "festival.bus", "festival.nodes", "festival.workloads", "festival.cnpg",
)
# 項目の依存先になる既存の情報源。鮮度の上限を [festival.max_age] に必ず宣言させる
FESTIVAL_MAX_AGE_SOURCES = FESTIVAL_SOURCE_IDS + (
    "monitor.uptimerobot", "monitor.healthchecks", "connect.tunnel", "ci.github", "mail.fail2ban",
)

Direction = Literal["above", "below"]  # above: 値が大きいほど悪い
THRESHOLD_DIRECTIONS: Mapping[str, Direction] = {
    "worker_error_rate": "above", "worker_exceeded": "above", "worker_cpu_p99_ms": "above",
    "cms_5xx_rate": "above", "cms_origin_p95_ms": "above", "media_4xx": "above", "media_5xx": "above",
    "parking_capture_age": "above", "parking_judge_failures": "above", "parking_judge_success_age": "above",
    "quake_cron_age": "above", "stamp_failed": "above", "stamp_d1_failed": "above",
    "stamp_rate_limited": "above", "bus_feed_age": "above", "cms_ready_ratio": "below",
    "pod_cpu_ratio": "above", "db_connections_ratio": "above", "db_replica_lag_s": "above",
    "db_backends": "above", "db_memory_rss_ratio": "above", "db_unanalyzed_tables": "above",
    "cpu_throttle_ratio": "above", "cms_rps": "above", "cms_cache_hit_ratio": "below",
    "node_cpu_ratio": "above", "node_memory_ratio": "above", "node_disk_ratio": "above",
    "tunnel_connections": "below",
}
DURATION_THRESHOLDS = frozenset({"parking_capture_age", "parking_judge_success_age", "quake_cron_age", "bus_feed_age"})
# 宣言が無い間は判定せず表示だけを行う閾値名
OPTIONAL_THRESHOLDS = frozenset({"cms_cache_hit_ratio"})
MIN_REQUESTS_KEYS = ("worker_error_rate", "cms_5xx_rate")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Plan:
    service: str
    name: str
    paid: bool
    from_: date
    until: date | None
    limits: Mapping[str, int | float]


@dataclass(frozen=True)
class Server:
    name: str
    until: date | None = None


@dataclass(frozen=True)
class Credential:
    name: str
    expires_on: date


@dataclass(frozen=True)
class Thresholds:
    warn_ratio: float = 0.8
    cert_warn_days: int = 21
    cert_crit_days: int = 7
    pod_restart_warn: int = 5
    disk_warn: float = 0.85
    disk_crit: float = 0.95
    memory_warn: float = 0.85
    backup_max_age: Mapping[str, timedelta] = field(default_factory=dict)
    volsync_max_age: timedelta = timedelta(hours=12)


@dataclass(frozen=True)
class Threshold:
    warn: float | timedelta
    crit: float | timedelta


@dataclass(frozen=True)
class CloudflareTargets:
    worker_script: str
    cms_host: str
    media_path_like: str
    hub_object_name: str


@dataclass(frozen=True)
class ParkingTarget:
    enabled: bool


@dataclass(frozen=True)
class SignageTarget:
    url: str


@dataclass(frozen=True)
class CmsTargets:
    namespace: str
    deployment: str
    pod_selector: str
    db_cluster: str
    service_base: str
    restart_window: timedelta


@dataclass(frozen=True)
class CloudflaredTarget:
    namespace: str
    pod_selector: str


@dataclass(frozen=True)
class FalcoTarget:
    window: timedelta
    warn_priority: str
    crit_priority: str


@dataclass(frozen=True)
class FestivalConfig:
    page_url: str
    renotify: timedelta
    source_failure_grace: timedelta
    max_age: Mapping[str, timedelta]
    thresholds: Mapping[str, Threshold]
    min_requests: Mapping[str, int]
    cloudflare: CloudflareTargets
    parking: ParkingTarget
    signage: SignageTarget
    quake_url: str
    bus_feed_base: str
    disabled_sources: list[str]
    cms: CmsTargets
    cloudflared: CloudflaredTarget
    uptime_monitors: Mapping[str, str]
    falco: FalcoTarget


@dataclass(frozen=True)
class Config:
    plans: tuple[Plan, ...]
    servers: tuple[Server, ...]
    thresholds: Thresholds
    credentials: tuple[Credential, ...]
    exclude: tuple[str, ...]
    sources: Mapping[str, Mapping[str, Any]]
    festival: FestivalConfig | None = None

    def active_plan(self, service: str, today: date) -> Plan | None:
        for p in self.plans:
            if p.service == service and p.from_ <= today and (p.until is None or today <= p.until):
                return p
        return None

    def source_settings(self, source_id: str) -> Mapping[str, Any]:
        return self.sources.get(source_id, {})


_DURATION = re.compile(r"^(\d+)([hdm])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_duration(s: Any, where: str) -> timedelta:
    m = _DURATION.match(s) if isinstance(s, str) else None
    if not m:
        raise ConfigError(f"{where}: 期間は 30m・36h・8d の形式で指定してください: {s!r}")
    return timedelta(**{_UNITS[m.group(2)]: int(m.group(1))})


def _date(v: Any, where: str) -> date:
    # tomllib は TOML のネイティブ日付も date で返すが、宣言は文字列に統一する。
    if not isinstance(v, str):
        raise ConfigError(f"{where}: 日付は YYYY-MM-DD の文字列で指定してください: {v!r}")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ConfigError(f"{where}: 日付の形式が誤っています: {v!r}") from None


def _require(d: Mapping, key: str, typ, where: str):
    if not isinstance(d.get(key), typ):
        raise ConfigError(f"{where}: {key} が必要です ({typ.__name__})")
    return d[key]


def _plans(raw) -> tuple[Plan, ...]:
    plans = []
    for n, p in enumerate(raw, 1):
        where = f"plans[{n}]"
        service = _require(p, "service", str, where)
        if service not in KNOWN_SERVICES:
            raise ConfigError(f"{where}: 未知のサービスです: {service}")
        limits = p.get("limits", {})
        if not isinstance(limits, dict) or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 for v in limits.values()):
            raise ConfigError(f"{where}: limits は 0 以上の数値の表にしてください")
        from_ = _date(p.get("from"), f"{where}.from")
        until = _date(p["until"], f"{where}.until") if "until" in p else None
        if until is not None and until < from_:
            raise ConfigError(f"{where}: until が from より前です")
        plans.append(Plan(service, _require(p, "name", str, where), _require(p, "paid", bool, where),
                          from_, until, limits))
    for i, a in enumerate(plans):
        for b in plans[i + 1:]:
            if a.service != b.service:
                continue
            a_end = a.until or date.max
            b_end = b.until or date.max
            if a.from_ <= b_end and b.from_ <= a_end:
                raise ConfigError(f"{a.service}: プラン「{a.name}」と「{b.name}」の期間が重複しています")
    return tuple(plans)


def _thresholds(raw: Mapping) -> Thresholds:
    unknown = set(raw) - set(Thresholds.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"thresholds: 未知のキー: {sorted(unknown)}")
    kw = dict(raw)
    if "backup_max_age" in kw:
        kw["backup_max_age"] = {k: parse_duration(v, f"thresholds.backup_max_age.{k}")
                                for k, v in kw["backup_max_age"].items()}
    if "volsync_max_age" in kw:
        kw["volsync_max_age"] = parse_duration(kw["volsync_max_age"], "thresholds.volsync_max_age")
    return Thresholds(**kw)


def _table(raw: Any, where: str, keys: tuple[str, ...]) -> Mapping[str, Any]:
    """キーが keys と過不足なく一致する表であることを確かめる。"""
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where}: 表が必要です")
    unknown = set(raw) - set(keys)
    if unknown:
        raise ConfigError(f"{where}: 未知のキー: {sorted(unknown)}")
    missing = [k for k in keys if k not in raw]
    if missing:
        raise ConfigError(f"{where}: 必要なキーがありません: {missing}")
    return raw


def _int(d: Mapping, key: str, where: str, minimum: int = 0) -> int:
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise ConfigError(f"{where}.{key}: {minimum} 以上の整数が必要です: {v!r}")
    return v


def _str(d: Mapping, key: str, where: str) -> str:
    if not isinstance(d[key], str) or not d[key]:
        raise ConfigError(f"{where}.{key}: 空でない文字列が必要です")
    return d[key]


def _threshold(name: str, raw: Any) -> Threshold:
    where = f"festival.thresholds.{name}"
    t = _table(raw, where, ("warn", "crit"))
    if name in DURATION_THRESHOLDS:
        warn, crit = (parse_duration(t[k], f"{where}.{k}") for k in ("warn", "crit"))
    else:
        for k in ("warn", "crit"):
            if isinstance(t[k], bool) or not isinstance(t[k], (int, float)):
                raise ConfigError(f"{where}.{k}: 数値が必要です: {t[k]!r}")
        warn, crit = t["warn"], t["crit"]
    # 向きごとに、注意が異常より軽い側にあること (等しいのは許す)
    if (warn > crit) if THRESHOLD_DIRECTIONS[name] == "above" else (warn < crit):
        raise ConfigError(f"{where}: 注意と異常が逆転しています (向き: {THRESHOLD_DIRECTIONS[name]})")
    return Threshold(warn, crit)


def parse_festival(raw: Mapping[str, Any]) -> FestivalConfig:
    top = _table(raw, "festival", (
        "page_url", "renotify", "source_failure_grace", "quake_url", "bus_feed_base", "disabled_sources",
        "max_age", "cloudflare", "parking", "signage", "cms", "cloudflared", "uptime_monitors", "falco",
        "min_requests", "thresholds"))
    known = set(KNOWN_SOURCE_IDS) | set(FESTIVAL_SOURCE_IDS)

    disabled = top["disabled_sources"]
    if not isinstance(disabled, list) or not all(isinstance(x, str) for x in disabled):
        raise ConfigError("festival.disabled_sources: 文字列の配列が必要です")
    bad = [x for x in disabled if x not in known]
    if bad:
        raise ConfigError(f"festival.disabled_sources: 未知の source_id: {bad}")

    age = top["max_age"]
    if not isinstance(age, Mapping):
        raise ConfigError("festival.max_age: 表が必要です")
    unknown_age = [k for k in age if k not in known]
    if unknown_age:
        raise ConfigError(f"festival.max_age: 未知の source_id: {unknown_age}")
    missing_age = [k for k in FESTIVAL_MAX_AGE_SOURCES if k not in age]
    if missing_age:
        raise ConfigError(f"festival.max_age: 鮮度の上限が欠けています: {missing_age}")
    max_age = {k: parse_duration(v, f"festival.max_age.{k}") for k, v in age.items()}

    th = top["thresholds"]
    if not isinstance(th, Mapping):
        raise ConfigError("festival.thresholds: 表が必要です")
    unknown_th = set(th) - set(THRESHOLD_DIRECTIONS)
    if unknown_th:
        raise ConfigError(f"festival.thresholds: 未知の閾値名: {sorted(unknown_th)}")
    absent = sorted(set(THRESHOLD_DIRECTIONS) - OPTIONAL_THRESHOLDS - set(th))
    if absent:
        raise ConfigError(f"festival.thresholds: 閾値名が欠けています: {absent}")
    thresholds = {name: _threshold(name, v) for name, v in th.items()}

    mr = _table(top["min_requests"], "festival.min_requests", MIN_REQUESTS_KEYS)
    min_requests = {k: _int(mr, k, "festival.min_requests") for k in MIN_REQUESTS_KEYS}

    cf = _table(top["cloudflare"], "festival.cloudflare", ("worker_script", "cms_host", "media_path_like", "hub_object_name"))
    pk = _table(top["parking"], "festival.parking", ("enabled",))
    if not isinstance(pk["enabled"], bool):
        raise ConfigError("festival.parking.enabled: 真偽値が必要です")
    sg = _table(top["signage"], "festival.signage", ("url",))
    cms = _table(top["cms"], "festival.cms", (
        "namespace", "deployment", "pod_selector", "db_cluster", "service_base", "restart_window"))
    cfd = _table(top["cloudflared"], "festival.cloudflared", ("namespace", "pod_selector"))
    um = _table(top["uptime_monitors"], "festival.uptime_monitors", ("idp", "cms_db"))
    fa = _table(top["falco"], "festival.falco", ("window", "warn_priority", "crit_priority"))
    for k in ("warn_priority", "crit_priority"):
        if fa[k] not in FALCO_PRIORITIES:
            raise ConfigError(f"festival.falco.{k}: 未知の優先度です: {fa[k]!r}")
    # PRIORITIES は重い順。異常の優先度は注意の優先度と同じかそれより重いこと
    if FALCO_PRIORITIES.index(fa["crit_priority"]) > FALCO_PRIORITIES.index(fa["warn_priority"]):
        raise ConfigError("festival.falco: crit_priority が warn_priority より軽くなっています")

    return FestivalConfig(
        page_url=_str(top, "page_url", "festival"),
        renotify=parse_duration(top["renotify"], "festival.renotify"),
        source_failure_grace=parse_duration(top["source_failure_grace"], "festival.source_failure_grace"),
        max_age=max_age, thresholds=thresholds, min_requests=min_requests,
        cloudflare=CloudflareTargets(*(_str(cf, k, "festival.cloudflare") for k in (
            "worker_script", "cms_host", "media_path_like", "hub_object_name"))),
        parking=ParkingTarget(pk["enabled"]),
        signage=SignageTarget(_str(sg, "url", "festival.signage")),
        quake_url=_str(top, "quake_url", "festival"), bus_feed_base=_str(top, "bus_feed_base", "festival"),
        disabled_sources=list(disabled),
        cms=CmsTargets(*(_str(cms, k, "festival.cms") for k in (
            "namespace", "deployment", "pod_selector", "db_cluster", "service_base")),
            parse_duration(cms["restart_window"], "festival.cms.restart_window")),
        cloudflared=CloudflaredTarget(_str(cfd, "namespace", "festival.cloudflared"),
                                      _str(cfd, "pod_selector", "festival.cloudflared")),
        uptime_monitors={k: _str(um, k, "festival.uptime_monitors") for k in ("idp", "cms_db")},
        falco=FalcoTarget(parse_duration(fa["window"], "festival.falco.window"), fa["warn_priority"], fa["crit_priority"]),
    )


def parse(text: str) -> Config:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML の構文エラー: {e}") from None
    servers = tuple(Server(_require(s, "name", str, f"servers[{n}]"),
                           _date(s["until"], f"servers[{n}].until") if "until" in s else None)
                    for n, s in enumerate(raw.get("servers", []), 1))
    creds = tuple(Credential(_require(c, "name", str, f"credentials[{n}]"),
                             _date(c.get("expires_on"), f"credentials[{n}].expires_on"))
                  for n, c in enumerate(raw.get("credentials", []), 1))
    sources = raw.get("sources", {})
    for sid in sources:
        if sid not in KNOWN_SOURCE_IDS:
            raise ConfigError(f"sources: 未知の情報源です: {sid}")
    return Config(_plans(raw.get("plans", [])), servers, _thresholds(raw.get("thresholds", {})), creds,
                  tuple(raw.get("exclude", {}).get("namespaces_or_names", [])), sources,
                  parse_festival(raw["festival"]) if "festival" in raw else None)


def load(path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"宣言ファイルを読めません: {e}") from None
    return parse(text)
