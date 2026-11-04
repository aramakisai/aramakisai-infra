import os
import unittest
from dataclasses import replace

import config
from helpers import FakeHttp, make_ctx
from http_client import HttpError
from model import Status
from sources import festival_quake as q

URL = "https://aramakisai-signage-quake.aramakisai.workers.dev/status"
ENV = {"SIGNAGE_QUAKE_STATUS_TOKEN": "tok"}

V2 = {
    "inPeriod": True,
    "period": {"from": "2026-11-01T00:00:00Z", "until": "2026-11-10T00:00:00Z"},
    "alert": {"active": False},
    "channels": {
        "p2p": {"connected": True, "lastReceivedAt": "2026-06-01T11:59:30Z", "receiving": True, "test": False},
        "jma_eew": {"connected": False, "lastReceivedAt": None, "receiving": False, "test": False},
    },
    "unreachable": False,
    "discord": {"configured": True, "disabled": False, "lastSuccessAt": "2026-06-01T10:00:00Z",
                "lastFailureAt": None, "lastFailure": None},
    "cron": {"lastRunAt": "2026-06-01T11:57:00Z"},
}


def run(body, cfg=None):
    http = FakeHttp({URL: body})
    return q.SOURCES[0].fetch(make_ctx(cfg=cfg, http=http, env=ENV)), http


class QuakeTest(unittest.TestCase):
    def test_v2_items(self):
        res, http = run(V2)
        by = {i.key: i for i in res.items}
        self.assertEqual(set(by), {"cron", "channel.p2p", "channel.jma_eew", "unreachable", "discord"})
        self.assertEqual(by["cron"].values, {"last_run_at": "2026-06-01T11:57:00Z", "in_period": True})
        self.assertEqual(by["channel.p2p"].values, {"connected": True, "receiving": True,
                                                    "last_received_at": "2026-06-01T11:59:30Z", "test": False})
        self.assertIsNone(by["channel.jma_eew"].values["last_received_at"])
        self.assertEqual(by["unreachable"].values, {"value": False})
        self.assertEqual(by["discord"].values, {
            "configured": True, "disabled": False, "last_success_at": "2026-06-01T10:00:00Z",
            "last_failure_at": None, "last_failure": None})
        self.assertTrue(all(i.status is Status.OK for i in res.items))
        self.assertEqual(http.calls[0]["bearer"], "tok")

    def test_old_relay_has_no_cron_item(self):
        old = {k: v for k, v in V2.items() if k != "cron"}
        res, _ = run(old)
        self.assertNotIn("cron", {i.key for i in res.items})
        self.assertIn("discord", {i.key for i in res.items})

    def test_null_last_run(self):
        res, _ = run({**V2, "cron": {"lastRunAt": None}})
        cron = next(i for i in res.items if i.key == "cron")
        self.assertIsNone(cron.values["last_run_at"])

    def test_unparsable_raises(self):
        with self.assertRaises(ValueError):
            run({"oops": 1})

    def test_http_error_raises(self):
        with self.assertRaises(HttpError):
            q.SOURCES[0].fetch(make_ctx(http=FakeHttp({URL: HttpError("GET -> 503", status=503)}), env=ENV))

    def test_undeclared_or_disabled_makes_no_request(self):
        cfg = config.load(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard.toml"))
        for c in (replace(cfg, festival=None),
                  replace(cfg, festival=replace(cfg.festival, disabled_sources=["festival.quake"]))):
            res, http = run(V2, c)
            self.assertEqual(res.status, Status.EMPTY)
            self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
