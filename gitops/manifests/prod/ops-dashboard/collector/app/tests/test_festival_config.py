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
        self.assertEqual(f.thresholds["worker_error_rate"], config.Threshold(0.02, None))
        self.assertEqual(f.thresholds["bus_feed_age"], config.Threshold(timedelta(minutes=2), timedelta(minutes=5)))
        self.assertEqual(f.thresholds["cms_ready_ratio"], config.Threshold(1.0, 0.5))
        self.assertEqual(f.thresholds["cms_5xx_rate"], config.Threshold(0.03, 0.10))
        for name in ("worker_error_rate", "worker_exceeded", "worker_cpu_p99_ms", "media_5xx", "db_memory_rss_ratio"):
            self.assertIsNone(f.thresholds[name].crit, name)
        self.assertEqual(config.OPTIONAL_THRESHOLDS, {
            "cms_rps", "cms_cache_hit_ratio", "stamp_failed", "stamp_d1_failed", "stamp_rate_limited",
            "db_replica_lag_s", "db_backends", "node_memory_ratio", "db_unanalyzed_tables",
            "parking_judge_failures", "parking_judge_success_age"})
        self.assertEqual(set(f.thresholds), set(config.THRESHOLD_DIRECTIONS) - config.OPTIONAL_THRESHOLDS)
        self.assertEqual(f.min_requests, {"worker_error_rate": 50, "cms_5xx_rate": 50})
        self.assertEqual(f.cms.restart_window, timedelta(minutes=30))
        self.assertEqual((f.cms.namespace, f.cms.pod_selector, f.cms.service_base),
                         ("prod", "app=cms", "http://cms.prod.svc.cluster.local:80"))
        self.assertEqual((f.cloudflared.namespace, f.cloudflared.pod_selector), ("cloudflared", "app=cloudflared"))
        self.assertFalse(f.parking.enabled)
        self.assertEqual(f.disabled_sources, [])
        self.assertEqual(f.falco.window, timedelta(minutes=15))

    def test_signage_service_token_is_declared_as_credential(self):
        cfg = config.load(PROD_TOML)
        self.assertIn("collector サイネージ Service Token", [c.name for c in cfg.credentials])

    def test_absent_declaration_is_none(self):
        self.assertIsNone(config.parse('[[servers]]\nname = "n"\n').festival)

    def test_optional_threshold_may_be_missing_or_declared(self):
        for name in sorted(config.OPTIONAL_THRESHOLDS):
            raw = raw_festival()
            self.assertNotIn(name, config.parse_festival(raw).thresholds, name)
        raw = raw_festival()
        raw["thresholds"]["cms_cache_hit_ratio"] = {"warn": 0.5, "crit": 0.2}
        self.assertEqual(config.parse_festival(raw).thresholds["cms_cache_hit_ratio"], config.Threshold(0.5, 0.2))

    def test_missing_required_threshold(self):
        raw = raw_festival()
        del raw["thresholds"]["cms_ready_ratio"]
        with self.assertRaisesRegex(config.ConfigError, "cms_ready_ratio"):
            config.parse_festival(raw)

    def test_unknown_threshold_name(self):
        raw = raw_festival()
        raw["thresholds"]["nope"] = {"warn": 1, "crit": 2}
        with self.assertRaisesRegex(config.ConfigError, "nope"):
            config.parse_festival(raw)

    def test_reversed_above_threshold(self):
        raw = raw_festival()
        raw["thresholds"]["cms_5xx_rate"] = {"warn": 0.2, "crit": 0.1}
        with self.assertRaisesRegex(config.ConfigError, "cms_5xx_rate"):
            config.parse_festival(raw)

    def test_reversed_below_threshold(self):
        raw = raw_festival()
        raw["thresholds"]["tunnel_connections"] = {"warn": 8, "crit": 12}
        with self.assertRaisesRegex(config.ConfigError, "tunnel_connections"):
            config.parse_festival(raw)

    def test_reversed_duration_threshold(self):
        raw = raw_festival()
        raw["thresholds"]["bus_feed_age"] = {"warn": "5m", "crit": "2m"}
        with self.assertRaisesRegex(config.ConfigError, "bus_feed_age"):
            config.parse_festival(raw)

    def test_equal_warn_and_crit_is_allowed(self):
        raw = raw_festival()
        raw["thresholds"]["cms_5xx_rate"] = {"warn": 0.1, "crit": 0.1}
        self.assertEqual(config.parse_festival(raw).thresholds["cms_5xx_rate"], config.Threshold(0.1, 0.1))

    def test_crit_may_be_omitted_and_skips_ordering_check(self):
        raw = raw_festival()
        raw["thresholds"]["cms_5xx_rate"] = {"warn": 0.5}
        raw["thresholds"]["tunnel_connections"] = {"warn": 12}
        raw["thresholds"]["bus_feed_age"] = {"warn": "9m"}
        th = config.parse_festival(raw).thresholds
        self.assertEqual(th["cms_5xx_rate"], config.Threshold(0.5, None))
        self.assertEqual(th["tunnel_connections"], config.Threshold(12, None))
        self.assertEqual(th["bus_feed_age"], config.Threshold(timedelta(minutes=9), None))

    def test_warn_is_required(self):
        for bad in ({}, {"crit": 0.1}):
            raw = raw_festival()
            raw["thresholds"]["cms_5xx_rate"] = bad
            with self.assertRaisesRegex(config.ConfigError, "warn", msg=bad):
                config.parse_festival(raw)

    def test_threshold_shape_and_types(self):
        for bad in ({"warn": 1, "crit": 2, "x": 3}, 5, {"warn": "a", "crit": 2}, {"warn": True, "crit": 2},
                    {"warn": 1, "crit": "a"}, {"warn": 1, "crit": None}):
            raw = raw_festival()
            raw["thresholds"]["cms_5xx_rate"] = bad
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

    def test_signage_installed_and_missing_threshold_are_rejected(self):
        raw = raw_festival()
        raw["signage"]["installed"] = 10
        with self.assertRaises(config.ConfigError):
            config.parse_festival(raw)
        raw = raw_festival()
        raw["thresholds"]["signage_missing"] = {"warn": 1, "crit": 3}
        with self.assertRaises(config.ConfigError):
            config.parse_festival(raw)

    def test_wrong_types(self):
        for path, key, bad in ((("parking",), "enabled", "no"), (("min_requests",), "cms_5xx_rate", 1.5),
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


class PrivateThresholdsTest(unittest.TestCase):
    def parse(self, private, raw=None):
        return config.parse_festival(raw or raw_festival(), private)

    def test_added_to_toml_thresholds(self):
        f = self.parse('{"cms_rps": {"warn": 11, "crit": 22}}')
        self.assertEqual(f.thresholds["cms_rps"], config.Threshold(11, 22))
        self.assertEqual(f.thresholds["cms_ready_ratio"], config.Threshold(1.0, 0.5))

    def test_crit_may_be_omitted(self):
        self.assertEqual(self.parse('{"cms_rps": {"warn": 11}}').thresholds["cms_rps"], config.Threshold(11, None))

    def test_duration_uses_toml_format(self):
        f = self.parse('{"bus_feed_age": {"warn": "1m", "crit": "3m"}}', _without("bus_feed_age"))
        self.assertEqual(f.thresholds["bus_feed_age"], config.Threshold(timedelta(minutes=1), timedelta(minutes=3)))

    def test_unset_or_empty_adds_nothing(self):
        for v in (None, ""):
            self.assertNotIn("cms_rps", self.parse(v).thresholds)

    def test_same_name_in_both_is_error(self):
        raw = raw_festival()
        raw["thresholds"]["cms_rps"] = {"warn": 1, "crit": 2}
        with self.assertRaises(config.ConfigError):
            self.parse('{"cms_rps": {"warn": 11}}', raw)

    def test_broken_or_non_object_json_is_error_without_value(self):
        for bad in ('{"cms_rps": {"warn": 9871', '[1]', '"s9871"', '9871', 'null'):
            with self.assertRaises(config.ConfigError) as cm:
                self.parse(bad)
            self.assertNotIn("9871", str(cm.exception), bad)

    def test_unknown_name_and_reversed_are_errors(self):
        for bad in ('{"nope": {"warn": 1}}', '{"cms_rps": {"warn": 22, "crit": 11}}',
                    '{"cms_rps": {"crit": 11}}', '{"cms_rps": {"warn": "x"}}'):
            with self.assertRaises(config.ConfigError, msg=bad):
                self.parse(bad)

    def test_parse_and_load_take_value(self):
        text = PROD_TOML.read_text(encoding="utf-8")
        self.assertIn("cms_rps", config.parse(text, '{"cms_rps": {"warn": 11, "crit": 22}}').festival.thresholds)
        self.assertIn("cms_rps", config.load(PROD_TOML, '{"cms_rps": {"warn": 11}}').festival.thresholds)
        self.assertNotIn("cms_rps", config.load(PROD_TOML).festival.thresholds)


def _without(name):
    raw = raw_festival()
    raw["thresholds"].pop(name, None)
    return raw


if __name__ == "__main__":
    unittest.main()
