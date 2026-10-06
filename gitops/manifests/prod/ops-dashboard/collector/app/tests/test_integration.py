import os
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta

import config
import main
import store as store_mod
from helpers import make_ctx
from model import Source, Status
from render import page
from sources import load_sources

PROD_TOML = os.path.join(os.path.dirname(__file__), "..", "..", "dashboard.toml")


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))
        self.addCleanup(self.store.close)

    def collect_all(self, sources):
        col = main.Collector(make_ctx(store=self.store), self.store, sources, timeout=5)
        for s in sources:
            col.collect(s)
        return col

    def test_prod_config_valid_and_all_sources_registered(self):
        config.load(PROD_TOML)
        ids = {s.source_id for s in load_sources()}
        self.assertEqual(ids, set(config.KNOWN_SOURCE_IDS))

    def test_every_source_failing_still_renders(self):
        sources = [s for s in load_sources() if s.interval is not None]
        col = self.collect_all(sources)
        self.assertTrue(all(r.status is Status.ERROR for r in col.results.values()))
        html = page.render(col.snapshot({}), {})
        self.assertIn("<html", html)

    def test_failing_source_does_not_change_others(self):
        sources = [s for s in load_sources() if s.interval is not None]
        base = self.collect_all(sources).snapshot({})
        victim = sources[0]

        def boom(ctx):
            raise RuntimeError("boom")

        broken = [replace(victim, fetch=boom) if s is victim else s for s in sources]
        other = self.collect_all(broken).snapshot({})
        for sid, r in base.items():
            if sid != victim.source_id:
                self.assertEqual((r.status, r.items, r.error), (other[sid].status, other[sid].items, other[sid].error))


if __name__ == "__main__":
    unittest.main()
