import dataclasses
import ipaddress
import unittest
from datetime import timedelta

import config
from helpers import FakeK8s, make_ctx
from http_client import HttpError
from model import Status
from sources import festival_nodes
from test_festival_config import raw_festival


def node(name, ip, ready="True", cidrs=("10.42.0.0/24",), cpu="4", mem="8Gi"):
    return {"metadata": {"name": name},
            "spec": {"podCIDRs": list(cidrs)},
            "status": {"addresses": [{"type": "InternalIP", "address": ip}, {"type": "Hostname", "address": name}],
                       "capacity": {"cpu": cpu, "memory": mem},
                       "conditions": [{"type": "Ready", "status": ready}]}}


NODES = {"items": [node("n1", "10.0.1.1"), node("n2", "10.0.1.2", ready="False", cidrs=("10.42.1.0/24",)),
                   node("n3", "10.0.1.3", cidrs=("10.42.2.0/24",))]}
METRICS = {"items": [{"metadata": {"name": "n1"}, "usage": {"cpu": "1", "memory": "4Gi"}},
                     {"metadata": {"name": "n2"}, "usage": {"cpu": "500m", "memory": "2Gi"}}]}


def stats(r):
    return {"node": {"fs": {"usedBytes": int(1000 * r), "capacityBytes": 1000}}}


def fetch(kubelet, ctx_kw=None):
    k8s = FakeK8s({"/api/v1/nodes": NODES, "/apis/metrics.k8s.io/v1beta1/nodes": METRICS}, kubelet=kubelet)
    return festival_nodes.SOURCES[0].fetch(make_ctx(k8s=k8s))


class NodesTest(unittest.TestCase):
    def test_items_and_internal_networks(self):
        r = fetch({"10.0.1.1": stats(0.5), "10.0.1.2": stats(0.25), "10.0.1.3": stats(0.1)})
        by = {i.key: i for i in r.items}
        self.assertEqual(set(by), {"node.n1", "node.n2", "node.n3"})
        self.assertEqual(by["node.n1"].values, {"ready": True, "cpu_ratio": 0.25, "memory_ratio": 0.5,
                                                "disk_ratio": 0.5, "addresses_count": 1})
        self.assertIs(by["node.n2"].values["ready"], False)
        self.assertEqual(by["node.n2"].values["cpu_ratio"], 0.125)
        # metrics-server に現れないノードは CPU・メモリだけ欠ける
        self.assertIsNone(by["node.n3"].values["cpu_ratio"])
        self.assertIsNone(by["node.n3"].values["memory_ratio"])
        self.assertEqual(by["node.n3"].values["disk_ratio"], 0.1)
        self.assertTrue(all(i.status == Status.OK for i in r.items))
        nets = festival_nodes.internal_networks()
        for a in ("10.0.1.1", "10.0.1.3", "10.42.1.77"):
            self.assertTrue(any(ipaddress.ip_address(a) in n for n in nets), a)
        self.assertFalse(any(ipaddress.ip_address("10.0.1.9") in n for n in nets))
        self.assertNotIn("10.0.1", repr([i.values for i in r.items]))

    def test_one_kubelet_unreachable_only_loses_that_disk(self):
        r = fetch({"10.0.1.1": stats(0.5), "10.0.1.2": HttpError("x"), "10.0.1.3": {"node": {}}})
        by = {i.key: i for i in r.items}
        self.assertIsNone(by["node.n2"].values["disk_ratio"])
        self.assertIsNotNone(by["node.n2"].note)
        self.assertIsNone(by["node.n3"].values["disk_ratio"])
        self.assertEqual(by["node.n1"].values["disk_ratio"], 0.5)
        self.assertIs(by["node.n2"].values["ready"], False)

    def test_undeclared_or_disabled_is_empty_without_requests(self):
        cfg = make_ctx().config
        raw = raw_festival()
        raw["disabled_sources"] = ["festival.nodes"]
        for c in (dataclasses.replace(cfg, festival=None),
                  dataclasses.replace(cfg, festival=config.parse_festival(raw))):
            r = festival_nodes.SOURCES[0].fetch(make_ctx(cfg=c, k8s=FakeK8s()))  # 問い合わせると KeyError
            self.assertEqual((r.status, list(r.items)), (Status.EMPTY, []))

    def test_interval(self):
        self.assertEqual(festival_nodes.SOURCES[0].interval, timedelta(seconds=60))


if __name__ == "__main__":
    unittest.main()
