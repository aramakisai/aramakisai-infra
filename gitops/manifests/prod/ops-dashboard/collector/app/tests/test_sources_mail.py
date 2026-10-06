import gzip
import unittest
from datetime import datetime, timedelta, timezone

from helpers import FIXED_NOW, FakeHttp, make_ctx, temp_store
from model import Status
from sources import mail
from test_report_ingest import DMARC_XML, TLS, mail as mk_mail
import json

BASE = mail.DEFAULT_URL
D1 = ("2026-06-01T11:00:00.123456+00:00 mx postfix/smtp[1]: AB12: to=<alice@example.org>, relay=mx.example.org[192.0.2.1]:25, "
      "delay=5, dsn=4.4.1, status=deferred (connect to mx.example.org[192.0.2.1]:25: Connection timed out)")
B1 = ("Jun  1 10:00:00 mx postfix/smtp[2]: CD34: to=<bob@example.net>, relay=none, delay=1, dsn=5.1.1, "
      "status=bounced (host mx.example.net said: 550 5.1.1 <bob@example.net>: Recipient address rejected)")


def src(sid):
    return next(s for s in mail.SOURCES if s.source_id == sid)


class Base(unittest.TestCase):
    def setUp(self):
        self.d, self.st = temp_store()

    def tearDown(self):
        self.st.close()
        self.d.cleanup()

    def ctx(self, routes, **kw):
        return make_ctx(http=FakeHttp(routes), env={"OPS_MAIL_AGENT_TOKEN": "tok"}, store=self.st, **kw)


class Delivery(Base):
    QUEUE = {"incoming": 0, "active": 1, "deferred": 2, "hold": 0,
             "oldest_deferred_mtime": FIXED_NOW.timestamp() - 7200}

    def test_events_saved_without_recipient_and_cursor_advances(self):
        ctx = self.ctx({f"{BASE}/queue": self.QUEUE, f"{BASE}/maillog": {"cursor": "1:99", "lines": [D1, B1]}})
        res = src("mail.delivery").fetch(ctx)
        rows = self.st.query("SELECT status, recipient_domain, reason FROM mail_events ORDER BY time")
        self.assertEqual([(r[0], r[1]) for r in rows], [("bounced", "example.net"), ("deferred", "example.org")])
        self.assertNotIn("@", "".join(r[2] for r in rows))
        self.assertEqual(self.st.get_cursor("mail_cursor"), "1:99")
        self.assertEqual(ctx.http.calls[0]["bearer"], "tok")
        q = next(i for i in res.items if i.key == "mail.queue")
        self.assertEqual((q.values["deferred"], q.values["oldest_deferred_age_s"]), (2, 7200))
        self.assertEqual(q.status, Status.WARN)
        series = {i.key: i.values for i in res.items if i.key.startswith("mail.series.")}
        self.assertEqual(series["mail.series.2026-06-01"], {"deferred": 1, "bounced": 1})
        top = [i.values for i in res.items if i.key.startswith("mail.top.")]
        self.assertEqual({(t["domain"], t["count"]) for t in top}, {("example.org", 1), ("example.net", 1)})

    def test_next_fetch_sends_cursor(self):
        self.st.set_cursor("mail_cursor", "1:99")
        calls = {}
        ctx = self.ctx({f"{BASE}/queue": self.QUEUE,
                        f"{BASE}/maillog": lambda **kw: calls.update(kw) or {"cursor": "2:5", "lines": []}})
        src("mail.delivery").fetch(ctx)
        self.assertEqual(calls["params"], {"cursor": "1:99"})
        self.assertEqual(self.st.get_cursor("mail_cursor"), "2:5")

    def test_healthy_queue_ok(self):
        ctx = self.ctx({f"{BASE}/queue": {"incoming": 0, "active": 0, "deferred": 0, "hold": 0,
                                          "oldest_deferred_mtime": None},
                        f"{BASE}/maillog": {"cursor": "1:0", "lines": []}})
        q = src("mail.delivery").fetch(ctx).items[0]
        self.assertEqual((q.status, q.values["oldest_deferred_age_s"]), (Status.OK, None))

    def test_parse_line_unparseable(self):
        self.assertIsNone(mail.parse_line("garbage status=deferred", FIXED_NOW))


class Fail2ban(Base):
    def test_only_active_bans(self):
        now = FIXED_NOW.timestamp()
        bans = [{"jail": "postfix", "ip": "192.0.2.1", "timeofban": now - 100, "bantime": 600, "bancount": 2},
                {"jail": "postfix", "ip": "192.0.2.2", "timeofban": now - 1000, "bantime": 600, "bancount": 1},
                {"jail": "dovecot", "ip": "192.0.2.3", "timeofban": now - 99999, "bantime": -1, "bancount": 1}]
        res = src("mail.fail2ban").fetch(self.ctx({f"{BASE}/fail2ban": {"bans": bans}}))
        got = {i.values["ip"]: i.values for i in res.items}
        self.assertEqual(set(got), {"192.0.2.1", "192.0.2.3"})
        self.assertEqual(got["192.0.2.1"]["until"], "2026-06-01T12:08:20Z")
        self.assertIsNone(got["192.0.2.3"]["until"])

    def test_no_bans_is_empty(self):
        res = src("mail.fail2ban").fetch(self.ctx({f"{BASE}/fail2ban": {"bans": []}}))
        self.assertEqual(res.status, Status.EMPTY)


class Reports(Base):
    def routes(self, msgs):
        r = {f"{BASE}/reports": {"keys": [{"key": k, "mtime": 0, "size": 1} for k in msgs]}}
        r.update({f"{BASE}/reports/{k}": v for k, v in msgs.items()})
        return r

    def test_ingest_dedup_and_reingest_after_db_loss(self):
        msgs = {"m1": mk_mail(gzip.compress(DMARC_XML), "r.gz", ("application", "gzip")),
                "m2": mk_mail(json.dumps(TLS).encode(), "r.json", ("application", "tlsrpt+json")),
                "m3": mk_mail(b"<feedback><bad", "r.xml", ("text", "xml"))}
        ctx = self.ctx(self.routes(msgs))
        res = src("mail.reports").fetch(ctx)
        self.assertEqual(self.st.query("SELECT count(*) FROM report_messages")[0][0], 3)
        fetched = [c["url"] for c in ctx.http.calls if "/reports/" in c["url"]]
        src("mail.reports").fetch(ctx)  # 2 回目は取得済みを再取得しない
        self.assertEqual([c["url"] for c in ctx.http.calls if "/reports/" in c["url"]], fetched)
        keys = {i.key for i in res.items}
        self.assertIn("dmarc.day.2026-06-01", keys)
        self.assertIn("tls.day.2026-06-01", keys)
        self.assertIn("ingest.failures", keys)
        day = next(i for i in res.items if i.key == "dmarc.day.2026-06-01")
        self.assertEqual((day.values["pass"], day.values["fail"]), (3, 2))
        recs = [i.values for i in res.items if i.key.startswith("dmarc.rec.")]
        self.assertEqual(sum(r["count"] for r in recs), 5)
        # DB 消失 -> 全メッセージを取り込み直すが、集計は重複しない
        self.st.write(lambda c: c.execute("DELETE FROM report_messages"))
        res = src("mail.reports").fetch(ctx)
        day = next(i for i in res.items if i.key == "dmarc.day.2026-06-01")
        self.assertEqual(day.values["pass"], 3)
        self.assertEqual(self.st.query("SELECT count(*) FROM report_messages")[0][0], 3)

    def test_missing_message_skipped(self):
        r = self.routes({"m1": b""})
        del r[f"{BASE}/reports/m1"]
        res = src("mail.reports").fetch(self.ctx(r))
        self.assertEqual(res.status, Status.EMPTY)


if __name__ == "__main__":
    unittest.main()
