import unittest

from helpers import FakeHttp, make_ctx
from model import Status
from sources import festival_schedule as fs

URL = "http://cms.prod.svc.cluster.local:80/api/globals/site_settings"
SETTINGS = {"name": "荒牧祭", "secret_like": "ignored", "parking_monitor": {"start": "09:00", "end": "17:30"},
            "event_days": [{"id": "a", "start_at": "2026-11-06T00:00:00.000Z", "end_at": "2026-11-06T09:00:00.000Z", "label": "1日目"},
                           {"id": "b", "start_at": "2026-11-07T00:00:00.000Z", "end_at": "2026-11-07T09:00:00.000Z", "label": None}]}


class ScheduleTest(unittest.TestCase):
    def test_items_only_monitor_and_event_days(self):
        http = FakeHttp({URL: SETTINGS})
        res = fs.SOURCES[0].fetch(make_ctx(http=http))
        by = {i.key: i.values for i in res.items}
        self.assertEqual(by, {"monitor": {"start": "09:00", "end": "17:30"},
                              "event_day.0": {"start_at": "2026-11-06T00:00:00.000Z", "end_at": "2026-11-06T09:00:00.000Z"},
                              "event_day.1": {"start_at": "2026-11-07T00:00:00.000Z", "end_at": "2026-11-07T09:00:00.000Z"}})
        self.assertEqual(http.calls[0]["params"], {"depth": "0"})
        self.assertNotIn("bearer", http.calls[0])
        self.assertEqual(res.status, Status.OK)

    def test_no_declaration_does_not_query(self):
        import dataclasses
        ctx = make_ctx(http=FakeHttp())
        ctx = dataclasses.replace(ctx, config=dataclasses.replace(ctx.config, festival=None))
        res = fs.SOURCES[0].fetch(ctx)
        self.assertEqual(res.status, Status.EMPTY)
        self.assertEqual(ctx.http.calls, [])

    def test_missing_monitor_raises(self):
        with self.assertRaises(Exception):
            fs.SOURCES[0].fetch(make_ctx(http=FakeHttp({URL: {"event_days": []}})))


if __name__ == "__main__":
    unittest.main()
