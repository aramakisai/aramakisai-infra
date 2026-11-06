import dataclasses
import unittest
from datetime import timedelta

import config
from helpers import FakeHttp, make_ctx
from http_client import HttpError
from model import Status
from sources import _promtext, festival_cnpg, festival_workloads as fw
from test_festival_config import raw_festival

PRIMARY = """# HELP cnpg_backends_total Number of backends
# TYPE cnpg_backends_total gauge
cnpg_backends_total{datname="payload",state="active",usename="app"} 3
cnpg_backends_total{datname="payload",state="idle",usename="app"} 12
cnpg_backends_total{datname="postgres",state="idle",usename="x"} 1
cnpg_pg_settings_setting{name="max_connections"} 100
cnpg_pg_settings_setting{name="shared_buffers"} 16384
cnpg_festival_unanalyzed_tables_count 4
"""
REPLICA = """cnpg_backends_total{datname="payload",state="idle",usename="app"} 2
cnpg_pg_settings_setting{name="max_connections"} 100
cnpg_pg_replication_lag 1.5
"""


def ep(pod, ip, role):
    return fw.DbEndpoint(pod, ip, role)


class PromtextTest(unittest.TestCase):
    def test_parse_samples(self):
        s = _promtext.parse_samples(PRIMARY + 'x_total{a="q\\"z,}"} 1e3\nnan_m NaN\n', ["cnpg_backends_total", "x_total", "nan_m"])
        self.assertEqual(sum(v for _, v in s["cnpg_backends_total"]), 16)
        self.assertEqual(s["cnpg_backends_total"][0][0], {"datname": "payload", "state": "active", "usename": "app"})
        self.assertEqual(s["x_total"], [({"a": 'q"z,}'}, 1000.0)])
        self.assertEqual(s["nan_m"], [])
        self.assertEqual(_promtext.parse_samples(PRIMARY, ["absent"]), {"absent": []})


class CnpgTest(unittest.TestCase):
    def setUp(self):
        fw._db_endpoints = ()

    def run_fetch(self, routes, endpoints):
        fw._db_endpoints = endpoints
        return festival_cnpg.SOURCES[0].fetch(make_ctx(http=FakeHttp(routes)))

    def test_values(self):
        r = self.run_fetch({"http://10.1.0.1:9187/metrics": PRIMARY.encode(),
                            "http://10.1.0.2:9187/metrics": REPLICA.encode()},
                           (ep("db-1", "10.1.0.1", "primary"), ep("db-2", "10.1.0.2", "replica")))
        by = {i.key: i for i in r.items}
        self.assertEqual(by["db.db-1"].values, {"role": "primary", "backends": 16, "max_connections": 100,
                                                "replication_lag_s": None, "unanalyzed_tables": 4})
        self.assertEqual(by["db.db-2"].values, {"role": "replica", "backends": 2, "max_connections": 100,
                                                "replication_lag_s": 1.5, "unanalyzed_tables": None})
        self.assertEqual(r.status, Status.OK)

    def test_unreachable_pod_is_stale_but_all_unreachable_raises(self):
        routes = {"http://10.1.0.1:9187/metrics": PRIMARY.encode(),
                  "http://10.1.0.2:9187/metrics": HttpError("GET -> 500", status=500)}
        r = self.run_fetch(routes, (ep("db-1", "10.1.0.1", "primary"), ep("db-2", "10.1.0.2", "replica")))
        by = {i.key: i for i in r.items}
        self.assertEqual(by["db.db-2"].status, Status.STALE)
        self.assertIsNotNone(by["db.db-2"].note)
        self.assertNotIn("10.1.0.2", repr(by["db.db-2"]))
        self.assertEqual(by["db.db-2"].values, {"role": "replica", "backends": None, "max_connections": None,
                                                "replication_lag_s": None, "unanalyzed_tables": None})
        routes["http://10.1.0.1:9187/metrics"] = HttpError("GET -> 500", status=500)
        with self.assertRaises(HttpError):
            self.run_fetch(routes, (ep("db-1", "10.1.0.1", "primary"), ep("db-2", "10.1.0.2", "replica")))

    def test_no_endpoints_yet_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_fetch({}, ())

    def test_undeclared_or_disabled_is_empty_without_requests(self):
        http = FakeHttp()
        fw._db_endpoints = (ep("db-1", "10.1.0.1", "primary"),)
        raw = raw_festival()
        raw["disabled_sources"] = ["festival.cnpg"]
        cfg = make_ctx().config
        for c in (dataclasses.replace(cfg, festival=None),
                  dataclasses.replace(cfg, festival=config.parse_festival(raw))):
            r = festival_cnpg.SOURCES[0].fetch(make_ctx(cfg=c, http=http))
            self.assertEqual((r.status, list(r.items)), (Status.EMPTY, []))
        self.assertEqual(http.calls, [])

    def test_interval(self):
        self.assertEqual(festival_cnpg.SOURCES[0].interval, timedelta(seconds=30))


if __name__ == "__main__":
    unittest.main()
