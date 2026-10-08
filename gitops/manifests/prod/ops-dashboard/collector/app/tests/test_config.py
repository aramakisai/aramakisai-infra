import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import config

BASE = """
[[plans]]
service = "cloudflare_workers"
name = "Free"
paid = false
from = "2026-01-01"
until = "2026-10-31"
limits = { requests_day = 100_000 }

[[plans]]
service = "cloudflare_workers"
name = "Paid"
paid = true
from = "2026-11-01"
until = "2026-11-30"
limits = { requests_month = 10_000_000 }

[[servers]]
name = "prod-node-1"
"""


class ParseTest(unittest.TestCase):
    def test_valid(self):
        cfg = config.parse(BASE)
        self.assertEqual(len(cfg.plans), 2)
        self.assertEqual(cfg.plans[1].from_, date(2026, 11, 1))
        self.assertEqual(cfg.servers[0].name, "prod-node-1")
        self.assertEqual(cfg.thresholds.warn_ratio, 0.8)

    def test_active_plan(self):
        cfg = config.parse(BASE)
        self.assertEqual(cfg.active_plan("cloudflare_workers", date(2026, 11, 15)).name, "Paid")
        self.assertIsNone(cfg.active_plan("cloudflare_workers", date(2026, 12, 1)))

    def test_overlap(self):
        bad = BASE.replace('from = "2026-11-01"', 'from = "2026-10-31"')
        with self.assertRaisesRegex(config.ConfigError, "重複"):
            config.parse(bad)

    def test_open_ended_overlap(self):
        bad = BASE.replace('until = "2026-10-31"\n', "")
        with self.assertRaisesRegex(config.ConfigError, "重複"):
            config.parse(bad)

    def test_unknown_service(self):
        with self.assertRaisesRegex(config.ConfigError, "未知のサービス"):
            config.parse(BASE.replace("cloudflare_workers", "nope", 1))

    def test_bad_date(self):
        with self.assertRaisesRegex(config.ConfigError, "日付"):
            config.parse(BASE.replace("2026-11-30", "2026-13-40"))

    def test_until_before_from(self):
        with self.assertRaisesRegex(config.ConfigError, "until"):
            config.parse(BASE.replace("2026-11-30", "2026-10-01"))

    def test_unknown_source_settings(self):
        with self.assertRaisesRegex(config.ConfigError, "未知の情報源"):
            config.parse(BASE + '\n[sources."x.y"]\na = 1\n')

    def test_duration(self):
        cfg = config.parse(BASE + '\n[thresholds]\nbackup_max_age = { "a/b" = "36h" }\nvolsync_max_age = "12h"\n')
        self.assertEqual(cfg.thresholds.backup_max_age["a/b"], timedelta(hours=36))
        self.assertEqual(cfg.thresholds.volsync_max_age, timedelta(hours=12))

    def test_bad_duration(self):
        with self.assertRaises(config.ConfigError):
            config.parse(BASE + '\n[thresholds]\nvolsync_max_age = "abc"\n')

    def test_invalid_toml(self):
        with self.assertRaises(config.ConfigError):
            config.parse("[[plans")

    def test_credentials_and_exclude(self):
        cfg = config.parse(BASE + '\n[[credentials]]\nname = "x"\nexpires_on = "2027-01-01"\n[exclude]\nnamespaces_or_names = ["a"]\n')
        self.assertEqual(cfg.credentials[0].expires_on, date(2027, 1, 1))
        self.assertEqual(cfg.exclude, ("a",))


class ProductionFileTest(unittest.TestCase):
    def test_production_toml_loads(self):
        path = Path(__file__).resolve().parents[2] / "dashboard.toml"
        cfg = config.load(path)
        self.assertEqual([s.name for s in cfg.servers], ["prod-node-1", "prod-node-2", "prod-node-3"])
        from plan import server_diff
        from helpers import FIXED_NOW
        running = [{"name": s.name, "status": "running"} for s in cfg.servers]
        self.assertEqual(server_diff(cfg, running, FIXED_NOW), ([], []))
        services = {p.service for p in cfg.plans}
        self.assertEqual(services, set(config.KNOWN_SERVICES))
        workers = [p for p in cfg.plans if p.service == "cloudflare_workers"]
        self.assertTrue(any(p.paid for p in workers))
        self.assertTrue(any(not p.paid for p in workers))

    def test_load_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(config.ConfigError):
                config.load(Path(d) / "missing.toml")


if __name__ == "__main__":
    unittest.main()
