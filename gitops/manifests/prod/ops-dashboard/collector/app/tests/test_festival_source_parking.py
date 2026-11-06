import unittest

from helpers import FakeHttp, make_ctx
from model import MissingSecret, Status
from sources import festival_parking as fp

URL = "http://cms.prod.svc.cluster.local:80/api/ops/parking-health"
CAM = {"id": 7, "name": "第1駐車場", "lot": "P1", "last_captured_at": "2026-06-01T11:58:00.000Z",
       "device_received_at": "2026-06-01T11:58:01.000Z", "judge_last_succeeded_at": "2026-06-01T11:58:05.000Z",
       "judge_last_failed_at": None, "judge_last_error": None, "judge_consecutive_failures": 0,
       "lot_status_updated_at": "2026-06-01T11:58:05.000Z"}


def ctx_enabled(http, enabled=True):
    ctx = make_ctx(http=http, env={"OPS_PARKING_HEALTH_TOKEN": "tok"})
    if enabled:
        import dataclasses
        f = ctx.config.festival
        cfg = dataclasses.replace(ctx.config, festival=dataclasses.replace(f, parking=dataclasses.replace(f.parking, enabled=True)))
        ctx = dataclasses.replace(ctx, config=cfg)
    return ctx


class ParkingTest(unittest.TestCase):
    def test_items_per_camera_with_bearer(self):
        http = FakeHttp({URL: {"generated_at": "x", "cameras": [CAM, {**CAM, "id": 8, "name": "B", "lot": None,
                                                                          "judge_consecutive_failures": 3,
                                                                          "judge_last_error": "boom"}]}})
        res = fp.SOURCES[0].fetch(ctx_enabled(http))
        by = {i.key: i for i in res.items}
        self.assertEqual(set(by), {"camera.7", "camera.8"})
        self.assertEqual(by["camera.7"].label, "第1駐車場")
        self.assertEqual(by["camera.7"].values["last_captured_at"], CAM["last_captured_at"])
        self.assertEqual(by["camera.8"].values["judge_consecutive_failures"], 3)
        self.assertIsNone(by["camera.8"].values["lot"])
        self.assertTrue(all(i.status is Status.OK for i in res.items))
        self.assertEqual(http.calls[0]["bearer"], "tok")

    def test_disabled_does_not_query(self):
        http = FakeHttp()
        res = fp.SOURCES[0].fetch(ctx_enabled(http, enabled=False))
        self.assertEqual(res.status, Status.EMPTY)
        self.assertEqual(http.calls, [])

    def test_bad_response_raises(self):
        with self.assertRaises(Exception):
            fp.SOURCES[0].fetch(ctx_enabled(FakeHttp({URL: {"error": "x"}})))

    def test_missing_token(self):
        ctx = ctx_enabled(FakeHttp({URL: {"cameras": []}}))
        import dataclasses
        with self.assertRaises(MissingSecret):
            fp.SOURCES[0].fetch(dataclasses.replace(ctx, env={}))


if __name__ == "__main__":
    unittest.main()
