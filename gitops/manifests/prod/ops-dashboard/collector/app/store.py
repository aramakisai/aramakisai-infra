import queue
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

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

    def purge(self, now: datetime) -> None:
        def run(c):
            for table, col, days in RETENTION:
                cutoff = model.iso(now - timedelta(days=days))
                c.execute(f"DELETE FROM {table} WHERE datetime({col}) < datetime(?)", (cutoff,))
            c.execute("DELETE FROM dmarc_records WHERE NOT EXISTS (SELECT 1 FROM dmarc_reports r "
                      "WHERE r.org_name = dmarc_records.org_name AND r.report_id = dmarc_records.report_id)")
        self.write(run)
