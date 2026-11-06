import os
import unittest
from dataclasses import replace
from datetime import datetime, timezone

import config
from helpers import FakeHttp, make_ctx
from http_client import HttpError
from model import Status
from sources import festival_bus as b

BASE = "https://api-public.odpt.org/api/v4/gtfs/realtime/odpt_Kan_etsuTransportation_AllLines_"


def varint(n):
    out = bytearray()
    while True:
        out.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)


def feed(ts, *, version=True, tail=b"\x12\x03abc"):
    header = (b"\x0a\x03" + b"2.0" if version else b"") + b"\x10\x00" + b"\x18" + varint(ts)
    return b"\x0a" + varint(len(header)) + header + tail


TS = 1780315190  # 2026-06-01T12:00:00Z の少し前


class ReadTest(unittest.TestCase):
    def test_reads_timestamp(self):
        self.assertEqual(b.read_header_timestamp(feed(TS)), TS)
        self.assertEqual(b.read_header_timestamp(feed(TS, version=False, tail=b"")), TS)

    def test_no_header_or_timestamp_fails(self):
        for raw in (b"", b"\x12\x03abc", b"\x0a\x00", b"\x0a\x03\x0a\x01x", b"\x0a\x05\x0a"):
            with self.assertRaises(ValueError):
                b.read_header_timestamp(raw)


class FetchTest(unittest.TestCase):
    def run_(self, routes, cfg=None):
        http = FakeHttp(routes)
        return b.SOURCES[0].fetch(make_ctx(cfg=cfg, http=http)), http

    def test_both_feeds(self):
        res, http = self.run_({BASE + "trip_update": feed(TS), BASE + "vehicle": feed(TS - 20)})
        by = {i.key: i for i in res.items}
        self.assertEqual(set(by), {"feed.trip_update", "feed.vehicle"})
        iso = datetime.fromtimestamp(TS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(by["feed.trip_update"].values, {"header_timestamp": iso})
        self.assertTrue(all(i.status is Status.OK for i in res.items))

    def test_one_failure_is_stale_with_reason(self):
        res, _ = self.run_({BASE + "trip_update": feed(TS), BASE + "vehicle": HttpError("GET -> 503", status=503)})
        by = {i.key: i for i in res.items}
        self.assertEqual(by["feed.trip_update"].status, Status.OK)
        self.assertEqual(by["feed.vehicle"].status, Status.STALE)
        self.assertIsNone(by["feed.vehicle"].values["header_timestamp"])
        self.assertIn("503", by["feed.vehicle"].note)

    def test_unparsable_feed_is_stale(self):
        res, _ = self.run_({BASE + "trip_update": feed(TS), BASE + "vehicle": b"\x12\x00"})
        self.assertEqual({i.key: i.status for i in res.items}["feed.vehicle"], Status.STALE)

    def test_both_failures_raise(self):
        with self.assertRaises(RuntimeError):
            self.run_({BASE + "trip_update": HttpError("x"), BASE + "vehicle": b""})

    def test_undeclared_or_disabled_makes_no_request(self):
        cfg = config.load(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard.toml"))
        for c in (replace(cfg, festival=None),
                  replace(cfg, festival=replace(cfg.festival, disabled_sources=["festival.bus"]))):
            res, http = self.run_({}, c)
            self.assertEqual(res.status, Status.EMPTY)
            self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
