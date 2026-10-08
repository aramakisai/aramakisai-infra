import json
import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from helpers import FIXED_NOW, FakeHttp, FakeK8s, make_ctx
from model import Status, iso
from sources import node_maintenance as nm
from sources import node_resources as nr

NODES = {"items": [{"metadata": {"name": "prod-node-1"},
                    "status": {"capacity": {"cpu": "4", "memory": "8Gi"},
                               "nodeInfo": {"kubeletVersion": "v1.32.3+k3s1"}}}]}
METRICS = {"items": [{"metadata": {"name": "prod-node-1"}, "usage": {"cpu": "1000000000n", "memory": "4Gi"}}]}


def node3(version="v1.32.3+k3s1", versions=None):
    names = ["prod-node-1", "prod-node-2", "prod-node-3"]
    vs = versions or [version] * 3
    nodes = {"items": [{"metadata": {"name": n},
                        "status": {"capacity": {"cpu": "4", "memory": "8Gi"}, "nodeInfo": {"kubeletVersion": v}}}
                       for n, v in zip(names, vs)]}
    metrics = {"items": [{"metadata": {"name": n}, "usage": {"cpu": f"{i + 1}000000000n", "memory": f"{i + 1}Gi"}}
                         for i, n in enumerate(names)]}
    return nodes, metrics


def statvfs(used_ratio):
    return mock.Mock(f_blocks=1000, f_bfree=int(1000 * (1 - used_ratio)), f_bavail=int(1000 * (1 - used_ratio)),
                     f_frsize=1)


class ResourcesTest(unittest.TestCase):
    def run_fetch(self, ratio):
        k8s = FakeK8s({"/api/v1/nodes": NODES, "/apis/metrics.k8s.io/v1beta1/nodes": METRICS})
        with mock.patch("os.statvfs", return_value=statvfs(ratio)):
            return {i.key: i for i in nr.SOURCES[0].fetch(make_ctx(k8s=k8s)).items}

    def test_quantity(self):
        self.assertEqual(nr.quantity("250m"), 0.25)
        self.assertEqual(nr.quantity("8Gi"), 8 * 2**30)
        self.assertEqual(nr.quantity("500000000n"), 0.5)
        self.assertEqual(nr.quantity("4"), 4)

    def test_ratios(self):
        by = self.run_fetch(0.5)
        self.assertEqual(by["cpu:prod-node-1"].values["ratio"], 25.0)
        self.assertEqual(by["memory:prod-node-1"].values["ratio"], 50.0)
        self.assertEqual(by["disk"].status, Status.OK)

    def test_disk_over_85_is_flagged(self):
        by = self.run_fetch(0.9)
        self.assertEqual(by["disk"].status, Status.WARN)
        self.assertIn("85%", by["disk"].note)
        self.assertEqual(self.run_fetch(0.96)["disk"].status, Status.CRIT)

    def test_three_nodes_each_get_cpu_and_memory(self):
        nodes, metrics = node3()
        k8s = FakeK8s({"/api/v1/nodes": nodes, "/apis/metrics.k8s.io/v1beta1/nodes": metrics})
        with mock.patch("os.statvfs", return_value=statvfs(0.5)):
            items = nr.SOURCES[0].fetch(make_ctx(k8s=k8s)).items
        by = {i.key: i for i in items}
        self.assertEqual(len(items), 7)
        self.assertEqual(by["cpu:prod-node-3"].values["ratio"], 75.0)
        self.assertEqual(by["memory:prod-node-2"].values["ratio"], 25.0)
        self.assertEqual(by["memory:prod-node-3"].label, "prod-node-3")
        self.assertEqual(by["disk"].label, "prod-node-1")

    def test_no_k8s_data_raises(self):
        with self.assertRaises(KeyError):
            nr.SOURCES[0].fetch(make_ctx())


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "node-status.json")
        self.addCleanup(self.dir.cleanup)

    def run_fetch(self, latest="v1.32.3+k3s1", state=None, nodes=NODES):
        if state is not None:
            with open(self.path, "w") as f:
                json.dump(state, f)
        http = FakeHttp({nm.CHANNELS_URL: {"data": [{"id": "latest", "latest": "v1.99.0+k3s1"},
                                                    {"id": "stable", "latest": latest}]}})
        with mock.patch.object(nm, "STATE_FILE", self.path):
            return {i.key: i for i in nm.SOURCES[0].fetch(
                make_ctx(http=http, k8s=FakeK8s({"/api/v1/nodes": nodes}))).items}

    def state(self, age=timedelta(hours=1), **kw):
        return {"generated_at": iso(FIXED_NOW - age), "upgradable_count": 3, "security_fixable_count": 0,
                "reboot_required": False, **kw}

    def test_up_to_date(self):
        by = self.run_fetch(state=self.state())
        self.assertEqual(by["k3s"].status, Status.OK)
        self.assertEqual(by["os"].status, Status.OK)
        self.assertEqual(by["os"].values["upgradable_count"], 3)

    def test_k3s_outdated(self):
        by = self.run_fetch(latest="v1.32.10+k3s1", state=self.state())
        self.assertEqual(by["k3s"].status, Status.WARN)
        self.assertEqual(by["k3s"].values["latest"], "v1.32.10+k3s1")

    def test_mixed_k3s_versions_use_oldest_and_warn(self):
        by = self.run_fetch(state=self.state(), nodes=node3(versions=["v1.32.3+k3s1", "v1.32.3+k3s1", "v1.31.0+k3s1"])[0])
        self.assertEqual(by["k3s"].status, Status.WARN)
        self.assertEqual(by["k3s"].values["running"], "v1.31.0+k3s1 / v1.32.3+k3s1")
        self.assertEqual(by["os"].label, "prod-node-1")

    def test_uniform_three_nodes_ok(self):
        by = self.run_fetch(state=self.state(), nodes=node3()[0])
        self.assertEqual(by["k3s"].status, Status.OK)
        self.assertEqual(by["k3s"].values["running"], "v1.32.3+k3s1")

    def test_pending_reboot(self):
        self.assertEqual(self.run_fetch(state=self.state(reboot_required=True))["os"].status, Status.WARN)

    def test_stale_file(self):
        by = self.run_fetch(state=self.state(age=timedelta(hours=49)))
        self.assertEqual(by["os"].status, Status.STALE)

    def test_null_security_count_keeps_k3s_item(self):
        by = self.run_fetch(state=self.state(security_fixable_count=None))
        self.assertEqual(by["os"].status, Status.OK)
        self.assertEqual(by["k3s"].status, Status.OK)

    def test_broken_state_file_is_isolated(self):
        with open(self.path, "w") as f:
            f.write("{broken")
        by = self.run_fetch()
        self.assertEqual(by["os"].status, Status.STALE)
        self.assertEqual(by["k3s"].status, Status.OK)

    def test_missing_file_is_stale(self):
        self.assertEqual(self.run_fetch()["os"].status, Status.STALE)


if __name__ == "__main__":
    unittest.main()
