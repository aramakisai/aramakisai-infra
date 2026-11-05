"""15 秒ごとに全項目を評価して SQLite に保存する。外部には問い合わせない (結果の写しと SQLite だけを読む)。"""
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, Sequence

import model
from config import FestivalConfig
from festival.catalog import CATALOG, EvalInput, ItemGroup
from model import FestivalItem, SourceResult, Status
from render.labels import label
from sources import festival_nodes

PARKING = "festival.parking"
# 情報源が取得遅延を示すために Item.note へ入れる文言。catalog はエラー文の表示にも note を使うので、これ以外の note では STALE にしない
STALE_NOTES = frozenset({"取得遅延", label("note.metrics_missing")})


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    failing_since: datetime | None  # ERROR または鮮度超過が始まった時刻。正常なら None
    reason: str | None


@dataclass(frozen=True)
class Evaluation:
    evaluated_at: datetime
    items: Sequence[FestivalItem]
    sources: Sequence[SourceHealth]


def log(event: str, **kv) -> None:
    print(json.dumps({"event": event, **kv}, ensure_ascii=False), flush=True)


def is_disabled(cfg: FestivalConfig, source_id: str) -> bool:
    return source_id in cfg.disabled_sources or (source_id == PARKING and not cfg.parking.enabled)


def _source_problem(cfg: FestivalConfig, sid: str, r: SourceResult | None, now: datetime) -> tuple[Status, str] | None:
    if r is None:
        return Status.ERROR, "未取得"
    if r.status is Status.ERROR:
        return Status.ERROR, r.error or "取得失敗"
    max_age = cfg.max_age.get(sid)
    if max_age is not None and now - r.fetched_at > max_age:
        return Status.STALE, "取得遅延"
    return None


class FestivalEvaluator:
    INTERVAL_SECONDS = 15

    def __init__(self, config: FestivalConfig, latest: Callable[[], Mapping[str, SourceResult]], store, notifier,
                 clock: Callable[[], datetime], heartbeat=None,
                 internal_networks: Callable[[], tuple] = festival_nodes.internal_networks):
        self.cfg = config
        self.latest = latest
        self.store = store
        self.notifier = notifier
        self.heartbeat = heartbeat
        self.clock = clock
        self.internal_networks = internal_networks
        self._failing: dict[str, datetime] = {}
        self._last_minute: datetime | None = None

    def _falco_counts(self, now: datetime) -> dict[str, int]:
        since = model.iso(now - self.cfg.falco.window)
        return {r["p"]: r["c"] for r in self.store.query(
            "SELECT lower(priority) AS p, count(*) AS c FROM falco_events WHERE received_at >= ? GROUP BY p",
            (since,)) if r["p"]}

    def _items_for(self, g: ItemGroup, inp: EvalInput, active: Sequence[str]) -> list[FestivalItem]:
        cfg, now = self.cfg, inp.now
        problems = [(s, _source_problem(cfg, s, inp.results.get(s), now)) for s in active]
        problems = [(s, p) for s, p in problems if p]
        fetched = [inp.results[s].fetched_at for s in active if s in inp.results]
        base = dict(category=g.category.value, source_ids=tuple(g.sources), link=g.link, link_label=g.link_label, notify=g.notify,
                    fetched_at=min(fetched) if fetched else None)
        judgements = list(g.judge(inp))
        errors = [p[1] for _, p in problems if p[0] is Status.ERROR]
        if not judgements and errors:
            # 結果が一度も無いと judge は何も返さないため、取得失敗の項目を 1 つ置く
            key = g.key_prefix.rstrip(".")
            return [FestivalItem(key=key, label=key, status=Status.ERROR, value="—", threshold=None,
                                 observed_at=None, note=errors[0], judged=True, **base)]
        out = []
        for j in judgements:
            status, note = j.status, j.note
            reasons = [p[1] for _, p in problems]
            if any(p[0] is Status.ERROR for _, p in problems):
                status, note = Status.ERROR, "; ".join(reasons)
            elif problems or j.note in STALE_NOTES:
                status = Status.STALE
                note = "; ".join(reasons) if reasons else j.note
            out.append(FestivalItem(key=j.key, label=j.label, status=status, value=j.value, threshold=j.threshold,
                                    observed_at=j.observed_at, note=note, judged=j.judged, metric=j.metric, unit=j.unit, warn=j.warn, crit=j.crit,
                                    breakdown=j.breakdown, **base))
        return out

    def _health(self, now: datetime, results: Mapping[str, SourceResult], used: set[str],
                alerts) -> list[SourceHealth]:
        out = []
        for sid in sorted(used):
            problem = _source_problem(self.cfg, sid, results.get(sid), now)
            if problem is None:
                self._failing.pop(sid, None)
                out.append(SourceHealth(sid, None, None))
                continue
            restored = alerts.get(f"source:{sid}")
            since = self._failing.setdefault(sid, restored.opened_at if restored else now)
            out.append(SourceHealth(sid, since, problem[1]))
        for sid in set(self._failing) - used:
            del self._failing[sid]
        return out

    def evaluate_once(self) -> Evaluation:
        now = self.clock()
        results = self.latest()
        state = self.store.load_festival_state()
        inp = EvalInput(now, self.cfg, results, self._falco_counts(now), self.internal_networks(), state,
                        notifier_configured=bool(getattr(self.notifier, "configured", False)),
                        heartbeat_configured=bool(getattr(self.heartbeat, "configured", False)))
        items: list[FestivalItem] = []
        used: set[str] = set()
        for g in CATALOG:
            active = [s for s in g.sources if not is_disabled(self.cfg, s)]
            # 駐車場の項目は festival.schedule (時間帯) にも依存するが、取得の無効は駐車場の項目全体を無効にする
            if g.sources and (not active or (PARKING in g.sources and is_disabled(self.cfg, PARKING))):
                continue  # 無効の情報源に依存する項目は作らない
            used.update(active)
            items.extend(self._items_for(g, inp, active))
        health = self._health(now, results, used, self.store.load_alert_states())
        minute = now.replace(second=0, microsecond=0)
        record = minute != self._last_minute
        history = [(i.key, i.metric) for i in items
                   if i.metric is not None and i.unit is not None
                   and i.status not in (Status.ERROR, Status.STALE, Status.EMPTY)] if record else []
        self.store.replace_festival_items(items, now, history, minute if record else None)
        # 書き込みが失敗した評価では分を消費せず、次の評価で記録し直す
        self._last_minute = minute
        return Evaluation(now, items, health)

    def tick(self) -> None:
        try:
            ev = self.evaluate_once()
        except Exception as e:
            reason = model.safe_error(e)
            log("festival_eval_failed", error=reason)
            try:
                self.store.save_festival_eval_error(reason)
            except Exception as e2:
                log("festival_eval_failed", error=model.safe_error(e2))
            return
        if self.notifier is not None:
            try:
                alerts = self.store.load_alert_states()
                notices = self.notifier.plan(ev, alerts, ev.evaluated_at)
                out = self.notifier.send(notices, ev.evaluated_at) if notices else None
                if out is not None and out.failure:
                    log("festival_notify_failed", reason=out.failure)
            except Exception as e:
                log("festival_notify_failed", error=model.safe_error(e))
        if self.heartbeat is not None:
            try:
                reason = self.heartbeat.beat(ev.evaluated_at)
                if reason:
                    log("festival_heartbeat_failed", reason=reason)
            except Exception as e:
                log("festival_heartbeat_failed", error=model.safe_error(e))

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(self.INTERVAL_SECONDS)
