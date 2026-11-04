import copy
import tomllib
import unittest
from datetime import timedelta
from pathlib import Path

import config

PROD_TOML = Path(__file__).resolve().parents[2] / "dashboard.toml"


def raw_festival() -> dict:
    return copy.deepcopy(tomllib.loads(PROD_TOML.read_text(encoding="utf-8"))["festival"])


class ParseFestivalTest(unittest.TestCase):
    def test_production_declaration_is_valid(self):
        cfg = config.load(PROD_TOML)
        f = cfg.festival
        self.assertIsNotNone(f)
        self.assertEqual(f.renotify, timedelta(minutes=30))
        self.assertEqual(f.max_age["festival.bus"], timedelta(minutes=2))
        self.assertEqual(f.thresholds["worker_error_rate"], config.Threshold(0.01, 0.05))
        self.assertEqual(f.thresholds["bus_feed_age"], config.Threshold(timedelta(minutes=2), timedelta(minutes=5)))
        self.assertEqual(f.thresholds["cms_ready_ratio"], config.Threshold(1.0, 0.5))
        self.assertNotIn("cms_cache_hit_ratio", f.thresholds)
        self.assertEqual(f.min_requests, {"worker_error_rate": 50, "cms_5xx_rate": 50})
        self.assertEqual(f.cms.restart_window, timedelta(minutes=30))
        self.assertEqual((f.cms.namespace, f.cms.pod_selector, f.cms.service_base),
                         ("prod", "app=cms", "http://cms.prod.svc.cluster.local:80"))
        self.assertEqual((f.cloudflared.namespace, f.cloudflared.pod_selector), ("cloudflared", "app=cloudflared"))
        self.assertFalse(f.parking.enabled)
        self.assertEqual(f.signage.installed, 0)
        self.assertEqual(f.disabled_sources, [])
        self.assertEqual(f.falco.window, timedelta(minutes=15))

    def test_signage_service_token_is_declared_as_credential(self):
        cfg = config.load(PROD_TOML)
        self.assertIn("collector サイネージ Service Token", [c.name for c in cfg.credentials])

    def test_absent_declaration_is_none(self):
        self.assertIsNone(config.parse('[[servers]]\nname = "n"\n').festival)

    def test_optional_threshold_may_be_missing_or_declared(self):
        raw = raw_festival()
        self.assertNotIn("cms_cache_hit_ratio", config.parse_festival(raw).thresholds)
        raw["thresholds"]["cms_cache_hit_ratio"] = {"warn": 0.5, "crit": 0.2}
        self.assertEqual(config.parse_festival(raw).thresholds["cms_cache_hit_ratio"], config.Threshold(0.5, 0.2))

    def test_missing_required_threshold(self):
        raw = raw_festival()
        del raw["thresholds"]["cms_rps"]
        with self.assertRaisesRegex(config.ConfigError, "cms_rps"):
            config.parse_festival(raw)

    def test_unknown_threshold_name(self):
        raw = raw_festival()
        raw["thresholds"]["nope"] = {"warn": 1, "crit": 2}
        with self.assertRaisesRegex(config.ConfigError, "nope"):
            config.parse_festival(raw)

    def test_reversed_above_threshold(self):
        raw = raw_festival()
        raw["thresholds"]["cms_rps"] = {"warn": 120, "crit": 80}
        with self.assertRaisesRegex(config.ConfigError, "cms_rps"):
            config.parse_festival(raw)

    def test_reversed_below_threshold(self):
        raw = raw_festival()
        raw["thresholds"]["tunnel_connections"] = {"warn": 1, "crit": 4}
        with self.assertRaisesRegex(config.ConfigError, "tunnel_connections"):
            config.parse_festival(raw)

    def test_reversed_duration_threshold(self):
        raw = raw_festival()
        raw["thresholds"]["bus_feed_age"] = {"warn": "5m", "crit": "2m"}
        with self.assertRaisesRegex(config.ConfigError, "bus_feed_age"):
            config.parse_festival(raw)

    def test_equal_warn_and_crit_is_allowed(self):
        raw = raw_festival()
        raw["thresholds"]["cms_rps"] = {"warn": 100, "crit": 100}
        self.assertEqual(config.parse_festival(raw).thresholds["cms_rps"], config.Threshold(100, 100))

    def test_threshold_needs_warn_and_crit_only(self):
        for bad in ({"warn": 1}, {"warn": 1, "crit": 2, "x": 3}, 5, {"warn": "a", "crit": 2}, {"warn": True, "crit": 2}):
            raw = raw_festival()
            raw["thresholds"]["cms_rps"] = bad
            with self.assertRaises(config.ConfigError, msg=bad):
                config.parse_festival(raw)

    def test_duration_threshold_must_be_duration(self):
        raw = raw_festival()
        raw["thresholds"]["bus_feed_age"] = {"warn": 2, "crit": 5}
        with self.assertRaises(config.ConfigError):
            config.parse_festival(raw)

    def test_unknown_key_in_every_table(self):
        for path in ((), ("cloudflare",), ("parking",), ("signage",), ("cms",), ("cloudflared",),
                     ("uptime_monitors",), ("falco",), ("min_requests",)):
            raw = raw_festival()
            t = raw
            for k in path:
                t = t[k]
            t["bogus"] = 1
            with self.assertRaisesRegex(config.ConfigError, "bogus", msg=path):
                config.parse_festival(raw)

    def test_missing_key_or_table(self):
        for path, key in (((), "page_url"), ((), "cms"), (("cms",), "deployment"), (("falco",), "window"),
                          (("min_requests",), "cms_5xx_rate"), (("uptime_monitors",), "idp")):
            raw = raw_festival()
            t = raw
            for k in path:
                t = t[k]
            del t[key]
            with self.assertRaises(config.ConfigError, msg=(path, key)):
                config.parse_festival(raw)

    def test_unknown_source_id_in_max_age(self):
        raw = raw_festival()
        raw["max_age"]["festival.nope"] = "3m"
        with self.assertRaisesRegex(config.ConfigError, "festival.nope"):
            config.parse_festival(raw)

    def test_max_age_must_cover_every_dependent_source(self):
        raw = raw_festival()
        del raw["max_age"]["festival.cnpg"]
        with self.assertRaisesRegex(config.ConfigError, "festival.cnpg"):
            config.parse_festival(raw)

    def test_unknown_source_id_in_disabled_sources(self):
        raw = raw_festival()
        raw["disabled_sources"] = ["festival.signage", "festival.nope"]
        with self.assertRaisesRegex(config.ConfigError, "festival.nope"):
            config.parse_festival(raw)

    def test_disabled_sources_accepts_festival_ids(self):
        raw = raw_festival()
        raw["disabled_sources"] = ["festival.signage"]
        self.assertEqual(config.parse_festival(raw).disabled_sources, ["festival.signage"])

    def test_bad_duration(self):
        raw = raw_festival()
        raw["renotify"] = "soon"
        with self.assertRaisesRegex(config.ConfigError, "renotify"):
            config.parse_festival(raw)

    def test_falco_priority_must_be_known_and_ordered(self):
        raw = raw_festival()
        raw["falco"]["crit_priority"] = "loud"
        with self.assertRaises(config.ConfigError):
            config.parse_festival(raw)
        raw = raw_festival()
        raw["falco"]["warn_priority"], raw["falco"]["crit_priority"] = "critical", "warning"
        with self.assertRaises(config.ConfigError):
            config.parse_festival(raw)

    def test_wrong_types(self):
        for path, key, bad in ((("parking",), "enabled", "no"), (("signage",), "installed", -1),
                               (("signage",), "installed", True), (("min_requests",), "cms_5xx_rate", 1.5),
                               ((), "page_url", 3), ((), "disabled_sources", "festival.bus")):
            raw = raw_festival()
            t = raw
            for k in path:
                t = t[k]
            t[key] = bad
            with self.assertRaises(config.ConfigError, msg=(key, bad)):
                config.parse_festival(raw)

    def test_festival_must_be_table(self):
        with self.assertRaises(config.ConfigError):
            config.parse("festival = 1\n")


if __name__ == "__main__":
    unittest.main()
