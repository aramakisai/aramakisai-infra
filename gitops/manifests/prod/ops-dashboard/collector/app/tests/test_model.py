import unittest
from datetime import datetime, timezone

import model
from model import Item, SourceResult, Status

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ModelTest(unittest.TestCase):
    def test_roundtrip_json(self):
        r = SourceResult("a.b", Status.WARN, T, T, (Item("k", "L", Status.OK, {"n": 1, "s": None}, "note"),), None)
        self.assertEqual(model.result_from_json(model.result_to_json(r)), r)

    def test_worst(self):
        self.assertEqual(model.worst([Status.OK, Status.CRIT, Status.WARN]), Status.CRIT)
        self.assertEqual(model.worst([Status.OK, Status.STALE]), Status.STALE)
        self.assertEqual(model.worst([]), Status.EMPTY)

    def test_safe_error_strips_query(self):
        msg = model.safe_error(RuntimeError("GET https://x/y?token=abc&a=b failed"))
        self.assertNotIn("abc", msg)
        self.assertIn("RuntimeError", msg)

    def test_make_result_aggregates(self):
        r = model.make_result("s", T, [Item("k", "L", Status.WARN, {}, None)])
        self.assertEqual(r.status, Status.WARN)
        self.assertEqual(r.last_success_at, T)
        self.assertIsNone(r.error)

    def test_iso(self):
        self.assertEqual(model.iso(T), "2026-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
