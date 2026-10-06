import unittest

from helpers import FakeHttp, make_ctx
from model import Status
from sources import monitor_healthchecks as hc
from sources import monitor_uptimerobot as ur


class UptimeRobotTest(unittest.TestCase):
    def test_states_and_quota(self):
        http = FakeHttp({ur.URL: {"stat": "ok", "pagination": {"offset": 0, "limit": 50, "total": 3}, "monitors": [
            {"id": 1, "friendly_name": "web", "status": 2},
            {"id": 2, "friendly_name": "api", "status": 9},
            {"id": 3, "friendly_name": "old", "status": 0}]}})
        res = ur.SOURCES[0].fetch(make_ctx(http=http, env={"OPS_UPTIMEROBOT_READONLY_KEY": "k"}))
        by = {i.key: i for i in res.items}
        self.assertEqual(by["monitor.1"].values["state"], "up")
        self.assertEqual(by["monitor.2"].status, Status.CRIT)
        self.assertEqual(by["monitor.3"].values["state"], "paused")
        self.assertEqual(by["quota"].values, {"used": 3, "limit": 50})
        self.assertEqual(res.status, Status.CRIT)
        self.assertEqual(http.calls[0]["data"]["api_key"], "k")

    def test_api_error_raises(self):
        http = FakeHttp({ur.URL: {"stat": "fail", "error": {"type": "invalid_parameter"}}})
        with self.assertRaises(RuntimeError):
            ur.SOURCES[0].fetch(make_ctx(http=http, env={"OPS_UPTIMEROBOT_READONLY_KEY": "k"}))

    def test_missing_secret(self):
        from model import MissingSecret
        with self.assertRaises(MissingSecret):
            ur.SOURCES[0].fetch(make_ctx())


class HealthchecksTest(unittest.TestCase):
    def test_states(self):
        http = FakeHttp({hc.URL: {"checks": [
            {"name": "backup", "uuid": "u1", "status": "up", "last_ping": "2026-06-01T11:00:00+00:00"},
            {"name": "mail", "uuid": "u2", "status": "grace", "last_ping": "2026-06-01T09:00:00+00:00"},
            {"name": "dr", "uuid": "u3", "status": "down", "last_ping": None},
            {"name": "x", "uuid": "u4", "status": "new", "last_ping": None}]}})
        res = hc.SOURCES[0].fetch(make_ctx(http=http, env={"OPS_HEALTHCHECKS_READONLY_KEY": "k"}))
        by = {i.key: i.status for i in res.items}
        self.assertEqual([by[f"check.u{n}"] for n in (1, 2, 3, 4)],
                         [Status.OK, Status.WARN, Status.CRIT, Status.STALE])
        self.assertEqual(http.calls[0]["headers"], {"X-Api-Key": "k"})
        quota = next(i for i in res.items if i.key == "quota")
        self.assertEqual(quota.values, {"used": 4, "limit": 20})


if __name__ == "__main__":
    unittest.main()
