import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

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
class Config:
    plans: tuple[Plan, ...]
    servers: tuple[Server, ...]
    thresholds: Thresholds
    credentials: tuple[Credential, ...]
    exclude: tuple[str, ...]
    sources: Mapping[str, Mapping[str, Any]]

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
                  tuple(raw.get("exclude", {}).get("namespaces_or_names", [])), sources)


def load(path) -> Config:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"宣言ファイルを読めません: {e}") from None
    return parse(text)
