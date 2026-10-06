import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import model
import store as store_mod
from model import Item, SourceResult, Status

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def days_ago(n, extra=0):
    return model.iso(NOW - timedelta(days=n, seconds=extra))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def count(self, table):
        return self.store.query(f"SELECT COUNT(*) FROM {table}")[0][0]

    def test_all_tables_exist(self):
        names = {r[0] for r in self.store.query("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {"source_results", "falco_events", "auth_events", "auth_cursor", "mail_events", "mail_cursor",
                    "report_messages", "dmarc_reports", "dmarc_records", "tlsrpt_reports", "ingest_failures"}
        self.assertTrue(expected <= names)

    def test_source_result_roundtrip(self):
        r = SourceResult("a", Status.OK, NOW, NOW, (Item("k", "L", Status.OK, {"x": 1}, None),), None)
        self.store.save_result(r)
        self.store.save_result(r)
        self.assertEqual(self.store.load_results(), {"a": r})

    def test_cursor(self):
        self.assertIsNone(self.store.get_cursor("auth_cursor"))
        self.store.set_cursor("auth_cursor", "5")
        self.store.set_cursor("auth_cursor", "9")
        self.assertEqual(self.store.get_cursor("auth_cursor"), "9")
        self.assertEqual(self.count("auth_cursor"), 1)

    def test_cursor_rejects_unknown_table(self):
        with self.assertRaises(ValueError):
            self.store.get_cursor("falco_events")

    def test_query_from_short_lived_threads_leaves_no_fds(self):
        import threading
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(50):
            t = threading.Thread(target=self.store.query, args=("SELECT 1",))
            t.start()
            t.join()
        self.assertLessEqual(len(os.listdir("/proc/self/fd")), before + 2)

    def test_insert_falco(self):
        self.store.insert_falco_event({"time": "t", "priority": "Notice", "rule": "r", "output": "o",
                                       "output_fields": {"k8s.ns.name": "ns", "k8s.pod.name": "p", "container.name": "c"}},
                                      received_at=NOW)
        row = self.store.query("SELECT rule,k8s_ns,k8s_pod,container FROM falco_events")[0]
        self.assertEqual(tuple(row), ("r", "ns", "p", "c"))

    def test_write_error_propagates_and_writer_survives(self):
        with self.assertRaises(Exception):
            self.store.write(lambda c: c.execute("INSERT INTO nope VALUES (1)"))
        self.store.save_result(SourceResult("a", Status.OK, NOW, NOW, (), None))
        self.assertEqual(self.count("source_results"), 1)

    def test_purge_boundaries(self):
        s = self.store
        s.insert_falco_event({"rule": "old", "output": ""}, received_at=NOW - timedelta(days=91))
        s.insert_falco_event({"rule": "new", "output": ""}, received_at=NOW - timedelta(days=89))
        s.write(lambda c: c.executemany(
            "INSERT INTO auth_events(sequence,created_at,event_type,user_id,login_name) VALUES (?,?,?,?,?)",
            [(1, days_ago(91), "e", "u", "l"), (2, days_ago(89), "e", "u", "l")]))
        s.write(lambda c: c.executemany(
            "INSERT INTO mail_events(time,status,recipient_domain,reason) VALUES (?,?,?,?)",
            [(days_ago(31), "deferred", "d", "r"), (days_ago(29), "bounced", "d", "r")]))
        s.write(lambda c: c.executemany(
            "INSERT INTO ingest_failures(source,key,time,reason) VALUES (?,?,?,?)",
            [("s", "k1", days_ago(31), "r"), ("s", "k2", days_ago(29), "r")]))
        s.write(lambda c: c.executemany(
            "INSERT INTO dmarc_reports(org_name,report_id,begin,end) VALUES (?,?,?,?)",
            [("o", "old", days_ago(402), days_ago(401)), ("o", "new", days_ago(399), days_ago(398))]))
        s.write(lambda c: c.executemany(
            "INSERT INTO dmarc_records(org_name,report_id,source_ip,count,disposition,dkim,spf,header_from) VALUES (?,?,?,?,?,?,?,?)",
            [("o", "old", "1.1.1.1", 1, "none", "pass", "pass", "d"), ("o", "new", "1.1.1.1", 1, "none", "pass", "pass", "d")]))
        s.write(lambda c: c.executemany(
            "INSERT INTO tlsrpt_reports(org_name,report_id,begin,end,policy_domain,success,failure) VALUES (?,?,?,?,?,?,?)",
            [("o", "old", days_ago(402), days_ago(401), "d", 1, 0), ("o", "new", days_ago(399), days_ago(398), "d", 1, 0)]))
        s.write(lambda c: c.executemany(
            "INSERT INTO report_messages(key,kind,ingested_at) VALUES (?,?,?)",
            [("old", "dmarc", days_ago(401)), ("new", "dmarc", days_ago(399))]))

        s.purge(NOW)

        self.assertEqual([r[0] for r in s.query("SELECT rule FROM falco_events")], ["new"])
        self.assertEqual([r[0] for r in s.query("SELECT sequence FROM auth_events")], [2])
        self.assertEqual([r[0] for r in s.query("SELECT status FROM mail_events")], ["bounced"])
        self.assertEqual([r[0] for r in s.query("SELECT key FROM ingest_failures")], ["k2"])
        self.assertEqual([r[0] for r in s.query("SELECT report_id FROM dmarc_reports")], ["new"])
        self.assertEqual([r[0] for r in s.query("SELECT report_id FROM dmarc_records")], ["new"])
        self.assertEqual([r[0] for r in s.query("SELECT report_id FROM tlsrpt_reports")], ["new"])
        self.assertEqual([r[0] for r in s.query("SELECT key FROM report_messages")], ["new"])

    def test_purge_mixed_timestamp_formats(self):
        self.store.write(lambda c: c.execute(
            "INSERT INTO mail_events(time,status,recipient_domain,reason) VALUES (?,?,?,?)",
            ("2026-04-01T00:00:00.123456+09:00", "deferred", "d", "r")))
        self.store.purge(NOW)
        self.assertEqual(self.count("mail_events"), 0)


if __name__ == "__main__":
    unittest.main()
