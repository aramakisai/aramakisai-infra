import os
import unittest
from dataclasses import replace

import config
from helpers import FakeHttp, make_ctx
from http_client import HttpError
from model import Status
from sources import festival_signage as s

URL = "https://aramakisai.com/api/signage/stats"
ENV = {"OPS_SIGNAGE_ACCESS_CLIENT_ID": "cid", "OPS_SIGNAGE_ACCESS_CLIENT_SECRET": "sec"}
STATS = {"at": "2026-06-01T11:59:50Z", "connections": {"prod": 7, "dev": 1, "preview": 0, "unknown": 2}}


def run(body, cfg=None):
    http = FakeHttp({URL: body})
    return s.SOURCES[0].fetch(make_ctx(cfg=cfg, http=http, env=ENV)), http


class SignageTest(unittest.TestCase):
    def test_item_and_headers(self):
        res, http = run(STATS)
        (item,) = res.items
        self.assertEqual(item.key, "connections")
        self.assertEqual(item.status, Status.OK)
        self.assertEqual(item.values, {"prod": 7, "dev": 1, "preview": 0, "unknown": 2, "at": "2026-06-01T11:59:50Z"})
        self.assertEqual(http.calls[0]["headers"], {"CF-Access-Client-Id": "cid", "CF-Access-Client-Secret": "sec"})

    def test_html_response_raises(self):
        # get_json が JSON でない本文で送り出す例外と同じ形
        with self.assertRaises(HttpError):
            s.SOURCES[0].fetch(make_ctx(http=FakeHttp({URL: HttpError("GET returned invalid JSON")}), env=ENV))

    def test_unexpected_shape_raises(self):
        for body in ("<html>login</html>", {"at": "x"}, {"at": "x", "connections": {"prod": "7"}}):
            with self.assertRaises(ValueError):
                run(body)

    def test_undeclared_or_disabled_makes_no_request(self):
        cfg = config.load(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard.toml"))
        for c in (replace(cfg, festival=None),
                  replace(cfg, festival=replace(cfg.festival, disabled_sources=["festival.signage"]))):
            res, http = run(STATS, c)
            self.assertEqual(res.status, Status.EMPTY)
            self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
