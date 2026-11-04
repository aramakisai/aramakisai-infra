import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import store as store_mod
from festival.heartbeat import FestivalHeartbeat
from http_client import HttpError, HttpResponse

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
URL = "https://hc-ping.test/SECRETUUID"


class FakeHttp:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request_meta(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0) if self.responses else HttpResponse(200, {}, b"OK")
        if isinstance(r, Exception):
            raise r
        return r


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_pings_at_most_once_per_60_seconds(self):
        http = FakeHttp()
        hb = FestivalHeartbeat(http, URL, self.store)
        for s in (0, 15, 30, 59):
            self.assertIsNone(hb.beat(NOW + timedelta(seconds=s)))
        self.assertEqual(len(http.calls), 1)
        hb.beat(NOW + timedelta(seconds=60))
        self.assertEqual(len(http.calls), 2)
        self.assertEqual(http.calls[0][:2], ("GET", URL))
        self.assertTrue(http.calls[0][2]["redact_url"])
        self.assertEqual(self.store.load_festival_state().heartbeat_ok_at, NOW + timedelta(seconds=60))

    def test_unconfigured_never_pings(self):
        http = FakeHttp()
        hb = FestivalHeartbeat(http, None, self.store)
        self.assertIsNone(hb.beat(NOW))
        self.assertEqual((http.calls, hb.configured), ([], False))

    def test_failure_returns_reason_records_state_and_is_not_retried_in_same_minute(self):
        http = FakeHttp(HttpError("GET -> 502", status=502), HttpError("GET failed: URLError"))
        hb = FestivalHeartbeat(http, URL, self.store)
        self.assertEqual(hb.beat(NOW), "http_502")
        self.assertIsNone(hb.beat(NOW + timedelta(seconds=15)))
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(hb.beat(NOW + timedelta(seconds=60)), "network")
        st = self.store.load_festival_state()
        self.assertEqual((st.heartbeat_failure, st.heartbeat_failed_at), ("network", NOW + timedelta(seconds=60)))
        self.assertNotIn("SECRETUUID", str(st))

    def test_failure_text_has_no_url_with_real_http(self):
        from http_client import Http
        hb = FestivalHeartbeat(Http(timeout=2), "http://127.0.0.1:9/SECRETUUID", self.store)
        self.assertEqual(hb.beat(NOW), "network")
        self.assertNotIn("SECRETUUID", str(self.store.load_festival_state()))


if __name__ == "__main__":
    unittest.main()
