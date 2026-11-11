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


def stats(r, rx=None, tx=None):
    node = {"fs": {"usedBytes": int(1000 * r), "capacityBytes": 1000}}
    if rx is not None:
        node["network"] = {"rxBytes": rx, "txBytes": tx}
    return {"node": node}


def fetch(kubelet, now=None):
    k8s = FakeK8s({"/api/v1/nodes": NODES, "/apis/metrics.k8s.io/v1beta1/nodes": METRICS}, kubelet=kubelet)
    return festival_nodes.SOURCES[0].fetch(make_ctx(k8s=k8s, **({"now": now} if now else {})))


class NodesTest(unittest.TestCase):
    def setUp(self):
        festival_nodes._net_prev.clear()

    def test_items_and_internal_networks(self):
        r = fetch({"10.0.1.1": stats(0.5), "10.0.1.2": stats(0.25), "10.0.1.3": stats(0.1)})
        by = {i.key: i for i in r.items}
        self.assertEqual(set(by), {"node.n1", "node.n2", "node.n3"})
        self.assertEqual(by["node.n1"].values, {"ready": True, "cpu_ratio": 0.25, "memory_ratio": 0.5,
                                                "disk_ratio": 0.5, "addresses_count": 1,
                                                "net_rx_bps": None, "net_tx_bps": None})
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

    def test_network_rate_from_counter_delta(self):
        t0 = make_ctx().clock()
        bps = lambda r: (r.items[0].values["net_rx_bps"], r.items[0].values["net_tx_bps"])
        self.assertEqual(bps(fetch({"10.0.1.1": stats(0.5, 1000, 500)}, t0)), (None, None))
        r = fetch({"10.0.1.1": stats(0.5, 7000, 2500)}, t0 + timedelta(seconds=60))
        self.assertEqual(bps(r), (100.0, 100 / 3))
        # カウンタ減少 (ノード再起動) は None で、新しい値が次回の基準になる
        self.assertEqual(bps(fetch({"10.0.1.1": stats(0.5, 10, 10)}, t0 + timedelta(seconds=120))), (None, None))
        r = fetch({"10.0.1.1": stats(0.5, 70, 70)}, t0 + timedelta(seconds=180))
        self.assertEqual(bps(r), (1.0, 1.0))
        # network 欠落は None
        self.assertEqual(bps(fetch({"10.0.1.1": stats(0.5)}, t0 + timedelta(seconds=240))), (None, None))

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
