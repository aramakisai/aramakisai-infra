import dataclasses
import json
import queue
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

import model

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_results (
    source_id TEXT PRIMARY KEY, json TEXT NOT NULL, fetched_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS falco_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, priority TEXT, rule TEXT NOT NULL, k8s_ns TEXT, k8s_pod TEXT,
    container TEXT, output TEXT, received_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS falco_events_received ON falco_events(received_at);
CREATE TABLE IF NOT EXISTS auth_events (
    sequence INTEGER NOT NULL, created_at TEXT NOT NULL, event_type TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '',
    login_name TEXT, PRIMARY KEY (user_id, sequence));
CREATE INDEX IF NOT EXISTS auth_events_created ON auth_events(created_at);
CREATE TABLE IF NOT EXISTS auth_cursor (id INTEGER PRIMARY KEY CHECK (id = 1), cursor TEXT);
CREATE TABLE IF NOT EXISTS mail_events (
    time TEXT NOT NULL, status TEXT NOT NULL, recipient_domain TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS mail_events_time ON mail_events(time);
CREATE TABLE IF NOT EXISTS mail_cursor (id INTEGER PRIMARY KEY CHECK (id = 1), cursor TEXT);
CREATE TABLE IF NOT EXISTS report_messages (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, ingested_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dmarc_reports (
    org_name TEXT NOT NULL, report_id TEXT NOT NULL, begin TEXT, end TEXT NOT NULL, PRIMARY KEY (org_name, report_id));
CREATE TABLE IF NOT EXISTS dmarc_records (
    org_name TEXT NOT NULL, report_id TEXT NOT NULL, source_ip TEXT, count INTEGER, disposition TEXT, dkim TEXT,
    spf TEXT, header_from TEXT);
CREATE INDEX IF NOT EXISTS dmarc_records_report ON dmarc_records(org_name, report_id);
CREATE TABLE IF NOT EXISTS tlsrpt_reports (
    org_name TEXT NOT NULL, report_id TEXT NOT NULL, begin TEXT, end TEXT NOT NULL, policy_domain TEXT,
    success INTEGER, failure INTEGER, PRIMARY KEY (org_name, report_id));
CREATE TABLE IF NOT EXISTS ingest_failures (
    source TEXT NOT NULL, key TEXT NOT NULL, time TEXT NOT NULL, reason TEXT);
CREATE INDEX IF NOT EXISTS ingest_failures_time ON ingest_failures(time);
CREATE TABLE IF NOT EXISTS festival_items (
    key TEXT PRIMARY KEY, category TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL,
    value TEXT NOT NULL, threshold TEXT, observed_at TEXT, fetched_at TEXT,
    source_ids TEXT NOT NULL, link TEXT, note TEXT, notify INTEGER NOT NULL,
    judged INTEGER NOT NULL DEFAULT 1, metric REAL, unit TEXT, warn REAL, crit REAL,
    breakdown TEXT, link_label TEXT);
CREATE TABLE IF NOT EXISTS festival_history (
    key TEXT NOT NULL, minute TEXT NOT NULL, value REAL NOT NULL,
    PRIMARY KEY (key, minute)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS festival_history_minute ON festival_history(minute);
CREATE TABLE IF NOT EXISTS festival_alerts (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, notified_open INTEGER NOT NULL,
    opened_at TEXT NOT NULL, last_notified_at TEXT);
CREATE TABLE IF NOT EXISTS festival_state (
    id INTEGER PRIMARY KEY CHECK (id = 1), evaluated_at TEXT, last_eval_error TEXT,
    last_success_at TEXT, last_failure_at TEXT, last_failure TEXT, not_before TEXT, halted INTEGER NOT NULL DEFAULT 0,
    heartbeat_ok_at TEXT, heartbeat_failed_at TEXT, heartbeat_failure TEXT);
"""

# (テーブル, 日時列, 保持日数)。SQLite の datetime() は Z・オフセット・小数秒付きの ISO 8601 を解釈するため、
# 取り込み元ごとに書式が揺れても比較できる。
RETENTION = (
    ("falco_events", "received_at", 90),
    ("auth_events", "created_at", 90),
    ("mail_events", "time", 30),
    ("ingest_failures", "time", 30),
    ("dmarc_reports", "end", 400),
    ("tlsrpt_reports", "end", 400),
    ("report_messages", "ingested_at", 400),
)



def _breakdown(s: str | None) -> model.Breakdown | None:
    if not s:
        return None
    d = json.loads(s)
    return model.Breakdown(d["title"], tuple(d["columns"]), tuple(tuple(r) for r in d["rows"]))


def _iso_or_none(dt: datetime | None) -> str | None:
    return model.iso(dt) if dt else None


def _parse_or_none(s: str | None) -> datetime | None:
    return model.parse_iso(s) if s else None


_CURSORS = {"auth_cursor": "cursor", "mail_cursor": "cursor"}


class Store:
    """書き込みは単一のライタースレッドに直列化する。読み取りは呼び出しごとの接続で並行に行う (WAL)。"""

    def __init__(self, path: str):
        self.path = path
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._init_error: BaseException | None = None
        self._writer = threading.Thread(target=self._run, name="store-writer", daemon=True)
        self._writer.start()
        self._ready.wait()
        if self._init_error:
            raise self._init_error

    def _run(self):
        try:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        except BaseException as e:
            self._init_error = e
            self._ready.set()
            return
        self._ready.set()
        while True:
            job = self._q.get()
            if job is None:
                break
            fn, box, done = job
            try:
                box.append((True, fn(conn)))
                conn.commit()
            except BaseException as e:
                conn.rollback()
                box.append((False, e))
            done.set()
        conn.close()

    def write(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        box: list = []
        done = threading.Event()
        self._q.put((fn, box, done))
        done.wait()
        ok, val = box[0]
        if not ok:
            raise val
        return val

    def query(self, sql: str, params=()) -> list[sqlite3.Row]:
        # 取得スレッドは短命なため、接続をスレッドに紐付けて残さない
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def close(self):
        self._q.put(None)
        self._writer.join()

    def save_result(self, r: model.SourceResult) -> None:
        self.write(lambda c: c.execute(
            "INSERT INTO source_results(source_id,json,fetched_at) VALUES (?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET json=excluded.json, fetched_at=excluded.fetched_at",
            (r.source_id, model.result_to_json(r), model.iso(r.fetched_at))))

    def load_results(self) -> dict[str, model.SourceResult]:
        return {row["source_id"]: model.result_from_json(row["json"])
                for row in self.query("SELECT source_id, json FROM source_results")}

    def get_cursor(self, table: str) -> str | None:
        col = _CURSORS[table] if table in _CURSORS else self._bad_cursor(table)
        rows = self.query(f"SELECT {col} FROM {table} WHERE id = 1")
        return rows[0][0] if rows else None

    def set_cursor(self, table: str, value: str) -> None:
        col = _CURSORS[table] if table in _CURSORS else self._bad_cursor(table)
        self.write(lambda c: c.execute(
            f"INSERT INTO {table}(id,{col}) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET {col}=excluded.{col}",
            (str(value),)))

    @staticmethod
    def _bad_cursor(table):
        raise ValueError(f"not a cursor table: {table}")

    def insert_falco_event(self, payload: Mapping[str, Any], received_at: datetime) -> None:
        f = payload.get("output_fields") or {}
        self.write(lambda c: c.execute(
            "INSERT INTO falco_events(time,priority,rule,k8s_ns,k8s_pod,container,output,received_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (payload.get("time"), payload.get("priority"), payload["rule"], f.get("k8s.ns.name"),
             f.get("k8s.pod.name"), f.get("container.name"), payload.get("output"), model.iso(received_at))))

    # --- 当日監視 (festival_*)。最新値だけを持ち、RETENTION の対象にしない ---

    @staticmethod
    def _set_festival_state(c: sqlite3.Connection, **cols) -> None:
        # 列名は呼び出し側のコード内の固定値で、外部入力を通さない
        c.execute("INSERT OR IGNORE INTO festival_state(id) VALUES (1)")
        c.execute(f"UPDATE festival_state SET {', '.join(f'{k}=?' for k in cols)} WHERE id = 1", tuple(cols.values()))

    def replace_festival_items(self, items: Sequence[model.FestivalItem], evaluated_at: datetime,
                               history: Sequence[tuple[str, float]] = (),
                               history_minute: datetime | None = None) -> None:
        def run(c):
            c.execute("DELETE FROM festival_items")
            c.executemany(
                "INSERT INTO festival_items(key,category,label,status,value,threshold,observed_at,fetched_at,"
                "source_ids,link,note,notify,judged,metric,unit,warn,crit,breakdown,link_label)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(i.key, i.category, i.label, str(i.status), i.value, i.threshold, _iso_or_none(i.observed_at),
                  _iso_or_none(i.fetched_at), ",".join(i.source_ids), i.link, i.note, int(i.notify), int(i.judged),
                  i.metric, i.unit, i.warn, i.crit,
                  json.dumps(dataclasses.asdict(i.breakdown), ensure_ascii=False) if i.breakdown else None,
                  i.link_label)
                 for i in items])
            if history and history_minute is not None:
                minute = model.iso(history_minute.replace(second=0, microsecond=0))
                c.executemany("INSERT OR REPLACE INTO festival_history(key,minute,value) VALUES (?,?,?)",
                              [(k, minute, v) for k, v in history])
                # minute は秒が 00 の同一書式の文字列なので、辞書順の比較が時刻順と一致する
                c.execute("DELETE FROM festival_history WHERE minute < ?",
                          (model.iso(history_minute - timedelta(hours=24)),))
            self._set_festival_state(c, evaluated_at=model.iso(evaluated_at), last_eval_error=None)
        self.write(run)

    def load_festival_items(self) -> list[model.FestivalItem]:
        return [model.FestivalItem(
            r["key"], r["category"], r["label"], model.Status(r["status"]), r["value"], r["threshold"],
            _parse_or_none(r["observed_at"]), _parse_or_none(r["fetched_at"]),
            tuple(r["source_ids"].split(",")) if r["source_ids"] else (), r["link"], r["note"],
            bool(r["notify"]), bool(r["judged"]), r["metric"], r["unit"], r["warn"], r["crit"],
            _breakdown(r["breakdown"]), r["link_label"])
            for r in self.query("SELECT * FROM festival_items ORDER BY key")]

    def load_festival_history(self, since: datetime, until: datetime,
                              key: str | None = None) -> dict[str, list[tuple[datetime, float]]]:
        sql = "SELECT key, minute, value FROM festival_history WHERE minute >= ? AND minute <= ?"
        args: tuple = (model.iso(since), model.iso(until))
        if key is not None:
            sql, args = sql + " AND key = ?", args + (key,)
        out: dict[str, list[tuple[datetime, float]]] = {}
        for r in self.query(sql + " ORDER BY key, minute", args):
            out.setdefault(r["key"], []).append((model.parse_iso(r["minute"]), r["value"]))
        return out

    def load_festival_state(self) -> model.FestivalStateRow:
        rows = self.query("SELECT * FROM festival_state WHERE id = 1")
        r = rows[0] if rows else {k: None for k in (
            "evaluated_at", "last_eval_error", "last_success_at", "last_failure_at", "last_failure", "not_before",
            "heartbeat_ok_at", "heartbeat_failed_at", "heartbeat_failure")} | {"halted": 0}
        return model.FestivalStateRow(
            _parse_or_none(r["evaluated_at"]), r["last_eval_error"],
            model.NotifierState(_parse_or_none(r["last_success_at"]), _parse_or_none(r["last_failure_at"]),
                                r["last_failure"], _parse_or_none(r["not_before"]), bool(r["halted"])),
            _parse_or_none(r["heartbeat_ok_at"]), _parse_or_none(r["heartbeat_failed_at"]), r["heartbeat_failure"])

    def save_festival_eval_error(self, error: str) -> None:
        """evaluated_at は動かさない (評価が止まっていることを画面が時刻の古さで示すため)。"""
        self.write(lambda c: self._set_festival_state(c, last_eval_error=error))

    def save_festival_heartbeat(self, at: datetime, ok: bool, failure: str | None) -> None:
        """成功と失敗の時刻は別々に残す (最終失敗が最終成功より新しいかで判定するため)。失敗理由は失敗時だけ更新する。"""
        if ok:
            self.write(lambda c: self._set_festival_state(c, heartbeat_ok_at=model.iso(at)))
        else:
            self.write(lambda c: self._set_festival_state(c, heartbeat_failed_at=model.iso(at), heartbeat_failure=failure))

    def load_alert_states(self) -> dict[str, model.AlertState]:
        return {r["key"]: model.AlertState(r["key"], r["kind"], r["label"], bool(r["notified_open"]),
                                           model.parse_iso(r["opened_at"]), _parse_or_none(r["last_notified_at"]))
                for r in self.query("SELECT * FROM festival_alerts")}

    def apply_notifications(self, upserts: Sequence[model.AlertState], deletes: Sequence[str],
                            notifier: model.NotifierState) -> None:
        """通知の状態の更新・削除と通知器の状態の更新を 1 回の書き込み (1 トランザクション) で行う。"""
        def run(c):
            c.executemany(
                "INSERT INTO festival_alerts(key,kind,label,notified_open,opened_at,last_notified_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, label=excluded.label, "
                "notified_open=excluded.notified_open, opened_at=excluded.opened_at, "
                "last_notified_at=excluded.last_notified_at",
                [(a.key, a.kind, a.label, int(a.notified_open), model.iso(a.opened_at),
                  _iso_or_none(a.last_notified_at)) for a in upserts])
            c.executemany("DELETE FROM festival_alerts WHERE key = ?", [(k,) for k in deletes])
            self._set_festival_state(
                c, last_success_at=_iso_or_none(notifier.last_success_at),
                last_failure_at=_iso_or_none(notifier.last_failure_at), last_failure=notifier.last_failure,
                not_before=_iso_or_none(notifier.not_before), halted=int(notifier.halted))
        self.write(run)

    def purge(self, now: datetime) -> None:
        def run(c):
            for table, col, days in RETENTION:
                cutoff = model.iso(now - timedelta(days=days))
                c.execute(f"DELETE FROM {table} WHERE datetime({col}) < datetime(?)", (cutoff,))
            c.execute("DELETE FROM dmarc_records WHERE NOT EXISTS (SELECT 1 FROM dmarc_reports r "
                      "WHERE r.org_name = dmarc_records.org_name AND r.report_id = dmarc_records.report_id)")
        self.write(run)
