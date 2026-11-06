import dataclasses
import unittest
from datetime import datetime, timezone

from helpers import FakeHttp, make_ctx
from model import Status
from sources import festival_stamp as fs

NOW = datetime(2026, 6, 1, 12, 0, 30, tzinfo=timezone.utc)
ENV = {"OPS_CLOUDFLARE_READ_TOKEN": "t", "TF_VAR_cloudflare_account_id": "ACC"}
URL = fs.AE_SQL.format(account="ACC")


def run(resp, cfg=None):
    http = FakeHttp({URL: resp})
    r = fs.SOURCES[0].fetch(make_ctx(cfg=cfg, http=http, env=ENV, now=NOW))
    return r, http


class StampTest(unittest.TestCase):
    def test_counts(self):
        r, http = run({"data": [{"failed": "12.0", "rate_limited": 7, "d1_failed": 3}]})
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(r.items[0].key, "stamp")
        self.assertEqual(r.items[0].values, {"failed": 12, "rate_limited": 7, "d1_failed": 3,
                                             "window_end": "2026-06-01T11:57:00Z"})
        sql = http.calls[0]["text"]
        self.assertIn("blob1 = 'prod'", sql)
        self.assertIn("2026-06-01 11:52:00", sql)
        self.assertIn("2026-06-01 11:57:00", sql)
        self.assertIn("_sample_interval", sql)
        self.assertEqual(http.calls[0]["bearer"], "t")

    def test_null_and_no_rows_are_zero(self):
        for resp in ({"data": [{"failed": None, "rate_limited": None, "d1_failed": None}]}, {"data": []}):
            r, _ = run(resp)
            self.assertEqual(r.items[0].values["failed"], 0)
            self.assertEqual(r.items[0].values["d1_failed"], 0)

    def test_bad_response_raises(self):
        with self.assertRaises(Exception):
            run({"errors": ["x"]})

    def test_no_festival_is_empty_without_call(self):
        cfg = dataclasses.replace(make_ctx().config, festival=None)
        r, http = run({"data": []}, cfg=cfg)
        self.assertEqual(r.status, Status.EMPTY)
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
