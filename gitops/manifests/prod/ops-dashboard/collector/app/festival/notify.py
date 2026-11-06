"""通知の差分計画 (plan、副作用なし) と当日専用 Discord webhook への送信 (send)。"""
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, Mapping, Sequence

from http_client import HttpError
from model import AlertState, NotifierState, Status

NoticeKind = Literal["alert", "reminder", "recover", "removed", "source_alert", "source_recover"]

MAX_CHARS = 2000
BACKOFF = timedelta(seconds=60)
HALT_STATUSES = (401, 403, 404)


@dataclass(frozen=True)
class Notice:
    key: str  # 項目キー、または "source:<source_id>"
    kind: NoticeKind
    label: str
    value: str | None = None
    threshold: str | None = None
    reason: str | None = None
    since: datetime | None = None  # 異常・情報源の失敗が始まった時刻
    warn: bool = False  # alert・reminder が注意の通知 (alert_on_warn の項目)


@dataclass(frozen=True)
class SendOutcome:
    sent: Sequence[Notice]
    deferred: Sequence[Notice]
    failure: str | None


def _line(n: Notice, grace_min: int) -> str:
    cur = f" — 現在 {n.value}" if n.value else ""
    th = f" ({n.threshold})" if n.threshold else ""
    mark = "🟡 注意" if n.warn else "🔴 異常"
    if n.kind == "alert":
        return f"{mark}: {n.label}{cur}{th}"
    if n.kind == "reminder":
        return f"{mark} (継続): {n.label}{cur}{th}"
    if n.kind == "recover":
        return f"🟢 回復: {n.label}{cur}"
    if n.kind == "removed":
        return f"⚪ 対象外: {n.label}"
    if n.kind == "source_alert":
        return f"🔴 異常: {n.label}の取得に失敗 ({grace_min} 分以上) — {n.reason}"
    return f"🟢 回復: {n.label}の取得が回復"


def _header(sent: Sequence[Notice]) -> str:
    count = lambda kinds: sum(1 for n in sent if n.kind in kinds)  # noqa: E731
    warn = sum(1 for n in sent if n.warn and n.kind in ("alert", "reminder"))
    parts = [f"異常 {c} 件" for c in [count(("alert", "reminder", "source_alert")) - warn] if c]
    parts += [f"注意 {warn} 件"] if warn else []
    parts += [f"回復 {c} 件" for c in [count(("recover", "source_recover"))] if c]
    parts += [f"対象外 {c} 件" for c in [count(("removed",))] if c]
    return "[当日監視] " + "・".join(parts)


class FestivalNotifier:
    def __init__(self, config, store, http, webhook_url: str | None):
        self.cfg = config
        self.store = store
        self.http = http
        self._url = webhook_url or None
        # 停止は「設定を直して再起動するまで」なので、起動時に解除して再送を許す
        st = store.load_festival_state().notifier
        if st.halted:
            store.apply_notifications([], [], replace(st, halted=False))

    @property
    def configured(self) -> bool:
        return self._url is not None

    def plan(self, evaluation, alerts: Mapping[str, AlertState], now: datetime) -> list[Notice]:
        cfg = self.cfg
        failing = {s.source_id for s in evaluation.sources if s.failing_since is not None}
        notices: list[Notice] = []
        seen: set[str] = set()
        for i in evaluation.items:
            if not (i.notify and i.judged):
                continue
            seen.add(i.key)
            if failing & set(i.source_ids):
                continue  # 失敗中の情報源に依存する項目は状態を凍結する (情報源の単位で通知する)
            a = alerts.get(i.key)
            opened = a is not None and a.notified_open
            if i.status is Status.CRIT or (i.alert_on_warn and i.status is Status.WARN):
                warn = i.status is Status.WARN
                if not opened:
                    notices.append(Notice(i.key, "alert", i.label, i.value, i.threshold, since=now, warn=warn))
                elif now - (a.last_notified_at or a.opened_at) >= cfg.renotify:
                    notices.append(Notice(i.key, "reminder", i.label, i.value, i.threshold, since=a.opened_at,
                                          warn=warn))
            elif i.status in (Status.OK, Status.EMPTY) and opened:
                notices.append(Notice(i.key, "recover", i.label, i.value))
            # alert_on_warn でない項目の注意と、取得遅延・取得失敗は状態を変えない (異常から注意への下降も回復にしない)
        for key, a in alerts.items():
            if a.kind == "item" and a.notified_open and key not in seen:
                notices.append(Notice(key, "removed", a.label))
        health = {s.source_id: s for s in evaluation.sources}
        for sid, s in health.items():
            key = f"source:{sid}"
            a = alerts.get(key)
            opened = a is not None and a.notified_open
            if s.failing_since is not None and not opened and now - s.failing_since >= cfg.source_failure_grace:
                notices.append(Notice(key, "source_alert", f"情報源 {sid}", reason=s.reason, since=s.failing_since))
            elif s.failing_since is None and opened:
                notices.append(Notice(key, "source_recover", f"情報源 {sid}"))
        for key, a in alerts.items():
            if a.kind == "source" and a.notified_open and key.removeprefix("source:") not in health:
                notices.append(Notice(key, "source_recover", a.label))
        return notices

    def _compose(self, notices: Sequence[Notice]) -> tuple[str, int]:
        grace_min = int(self.cfg.source_failure_grace.total_seconds() // 60)
        footer = f"\n{self.cfg.page_url}"
        lines: list[str] = []
        text = ""
        for n in notices:
            cand = lines + [_line(n, grace_min)]
            body = _header(notices[:len(cand)]) + "\n" + "\n".join(cand) + footer
            if len(body) > MAX_CHARS and lines:
                break
            text, lines = body[:MAX_CHARS], cand
        return text, len(lines)

    def send(self, notices: Sequence[Notice], now: datetime) -> SendOutcome:
        if not notices or self._url is None:
            return SendOutcome((), tuple(notices), None)
        st = self.store.load_festival_state().notifier
        if st.halted or (st.not_before and now < st.not_before):
            return SendOutcome((), tuple(notices), None)
        text, n = self._compose(notices)
        sent, deferred = tuple(notices[:n]), tuple(notices[n:])
        body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}, ensure_ascii=False).encode()
        try:
            resp = self.http.request_meta("POST", self._url, data=body, content_type="application/json",
                                          timeout=10, redact_url=True)
        except HttpError as e:
            return self._failed(st, e, notices, now)
        not_before = None
        if resp.headers.get("x-ratelimit-remaining") == "0":
            try:
                not_before = now + timedelta(seconds=float(resp.headers.get("x-ratelimit-reset-after", "")))
            except ValueError:
                not_before = now + BACKOFF
        self._apply(sent, now, NotifierState(now, st.last_failure_at, st.last_failure, not_before, False))
        return SendOutcome(sent, deferred, None)

    def _apply(self, sent: Sequence[Notice], now: datetime, state: NotifierState) -> None:
        existing = self.store.load_alert_states()
        upserts, deletes = [], []
        for n in sent:
            if n.kind in ("alert", "reminder", "source_alert"):
                kind = "source" if n.kind == "source_alert" else "item"
                prev = existing.get(n.key)
                opened = n.since or (prev.opened_at if prev else now)
                upserts.append(AlertState(n.key, kind, n.label, True, opened, now))
            else:
                deletes.append(n.key)
        self.store.apply_notifications(upserts, deletes, state)

    def _failed(self, st: NotifierState, e: HttpError, notices, now: datetime) -> SendOutcome:
        # 理由は状態コードか network・timeout だけ。メッセージには URL が入らない (redact_url)
        if e.status is None:
            reason = "timeout" if "Timeout" in str(e) else "network"
        else:
            reason = f"http_{e.status}"
        halted = e.status in HALT_STATUSES
        if halted:
            reason = f"disabled_{reason}"
        wait = timedelta(seconds=e.retry_after) if e.status == 429 and e.retry_after is not None else BACKOFF
        self.store.apply_notifications([], [], NotifierState(
            st.last_success_at, now, reason, None if halted else now + wait, halted))
        return SendOutcome((), tuple(notices), reason)
