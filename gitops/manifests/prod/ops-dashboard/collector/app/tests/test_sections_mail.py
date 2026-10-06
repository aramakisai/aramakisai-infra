import gzip
import json
import unittest

from helpers import FIXED_NOW, FakeHttp, make_ctx, temp_store
from model import Item, SourceResult, Status
from render.sections import dmarc, fail2ban, mail as mail_sec
from sources import mail
from test_report_ingest import DMARC_XML, TLS, mail as mk_mail
from test_sources_mail import B1, BASE, D1

NOW_ISO = FIXED_NOW.timestamp()


class Sections(unittest.TestCase):
    def setUp(self):
        self.d, self.st = temp_store()
        msgs = {"m1": mk_mail(gzip.compress(DMARC_XML), "r.gz", ("application", "gzip")),
                "m2": mk_mail(json.dumps(TLS).encode(), "r.json", ("application", "tlsrpt+json"))}
        routes = {f"{BASE}/queue": {"incoming": 0, "active": 0, "deferred": 1, "hold": 0,
                                    "oldest_deferred_mtime": NOW_ISO - 5400},
                  f"{BASE}/maillog": {"cursor": "1:1", "lines": [D1, B1]},
                  f"{BASE}/fail2ban": {"bans": [{"jail": "postfix", "ip": "192.0.2.9", "timeofban": NOW_ISO - 10,
                                                 "bantime": -1, "bancount": 1}]},
                  f"{BASE}/reports": {"keys": [{"key": k} for k in msgs]}}
        routes.update({f"{BASE}/reports/{k}": v for k, v in msgs.items()})
        ctx = make_ctx(http=FakeHttp(routes), env={"OPS_MAIL_AGENT_TOKEN": "t"}, store=self.st)
        self.snap = {s.source_id: s.fetch(ctx) for s in mail.SOURCES}

    def tearDown(self):
        self.st.close()
        self.d.cleanup()

    def test_mail(self):
        h = mail_sec.render(self.snap, {})
        for s in ("1:30", "example.net", "TLS"):
            self.assertIn(s, h)
        self.assertNotIn("@", h.replace("<address", ""))

    def test_fail2ban(self):
        h = fail2ban.render(self.snap, {})
        self.assertIn("192.0.2.9", h)
        self.assertIn("無期限", h)

    def test_dmarc_range_and_sources(self):
        h = dmarc.render(self.snap, {"dmarc": ["30d"]})
        self.assertIn("192.0.2.1", h)
        self.assertIn("198.51.100.7", h)
        self.assertIn("拒否", h)

    def test_dmarc_empty_and_collecting(self):
        self.assertIn("収集中", dmarc.render({}, {}))
        empty = SourceResult("mail.reports", Status.EMPTY, FIXED_NOW, FIXED_NOW, (), None)
        self.assertIn("該当するデータはありません", dmarc.render({"mail.reports": empty}, {}))

    def test_error_result_does_not_crash(self):
        err = SourceResult("mail.fail2ban", Status.ERROR, FIXED_NOW, None, (), "x")
        self.assertIn("x", fail2ban.render({"mail.fail2ban": err}, {}))


if __name__ == "__main__":
    unittest.main()
