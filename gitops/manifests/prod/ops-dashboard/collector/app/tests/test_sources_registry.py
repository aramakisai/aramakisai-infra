import sys
import types
import unittest
from datetime import timedelta

import sources
from model import Source


def fake_module(name, source_ids):
    m = types.ModuleType(name)
    m.SOURCES = tuple(Source(sid, timedelta(minutes=1), lambda ctx: None) for sid in source_ids)
    return m


class RegistryTest(unittest.TestCase):
    def test_load(self):
        sys.modules["sources.fake_a"] = fake_module("sources.fake_a", ["a.one", "a.two"])
        try:
            got = sources.load_sources(["fake_a"])
        finally:
            del sys.modules["sources.fake_a"]
        self.assertEqual([s.source_id for s in got], ["a.one", "a.two"])

    def test_duplicate_ids_rejected(self):
        sys.modules["sources.fake_b"] = fake_module("sources.fake_b", ["dup"])
        sys.modules["sources.fake_c"] = fake_module("sources.fake_c", ["dup"])
        try:
            with self.assertRaises(ValueError):
                sources.load_sources(["fake_b", "fake_c"])
        finally:
            del sys.modules["sources.fake_b"], sys.modules["sources.fake_c"]

    def test_registered_modules_importable(self):
        got = sources.load_sources()
        import config
        for s in got:
            self.assertIn(s.source_id, config.KNOWN_SOURCE_IDS)


if __name__ == "__main__":
    unittest.main()
