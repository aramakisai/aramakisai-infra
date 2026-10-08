import unittest

from helpers import FIXED_NOW
from model import Item, SourceResult, Status, make_result
from render.sections import cluster, data, monitoring, node

STALE_ERR = SourceResult("x", Status.ERROR, FIXED_NOW, None, (), "boom")


def res(sid, *items):
    return make_result(sid, FIXED_NOW, items)


class Monitoring(unittest.TestCase):
    def test_states_and_netdata_link(self):
        snap = {
            "monitor.uptimerobot": res(
                "monitor.uptimerobot", Item("quota", "uptimerobot", Status.OK, {"used": 1, "limit": 50}),
                Item("monitor.1", "site-a", Status.OK, {"state": "up"}),
                Item("monitor.2", "site-b", Status.CRIT, {"state": "down"})),
            "monitor.healthchecks": res(
                "monitor.healthchecks", Item("quota", "healthchecks", Status.OK, {"used": 1, "limit": 20}),
                Item("check.a", "backup-job", Status.WARN, {"state": "grace", "last_ping": "2026-06-01T11:00:00+00:00"}),
                Item("check.b", "never", Status.STALE, {"state": "new", "last_ping": None})),
        }
        h = monitoring.render(snap, {})
        for s in ("site-a", "稼働中", "site-b", "停止", "猶予中", "未開始", "2026-06-01 20:00", "Netdata で詳細を見る", "異常"):
            self.assertIn(s, h)
        self.assertNotIn("uptimerobot</td>", h)

    def test_error_and_collecting(self):
        h = monitoring.render({"monitor.uptimerobot": STALE_ERR}, {})
        self.assertIn("取得失敗", h)
        self.assertIn("boom", h)
        self.assertIn("収集中", h)


class Node(unittest.TestCase):
    def snap(self, disk=91.0, os_item=None):
        return {
            "node.resources": res(
                "node.resources",
                Item("cpu:prod-node-1", "prod-node-1", Status.OK, {"ratio": 12.5, "used": 0.5, "total": 4.0}),
                Item("memory:prod-node-1", "prod-node-1", Status.OK, {"ratio": 50.0, "used": 4e9, "total": 8e9}),
                Item("disk", "prod-node-1", Status.CRIT, {"ratio": disk, "used": 91e9, "total": 100e9},
                     "ディスク使用率が 85% を超えています (Discord 通知の対象)")),
            "node.maintenance": res(
                "node.maintenance",
                Item("k3s", "k3s", Status.WARN, {"running": "v1.31.0+k3s1", "latest": "v1.32.3+k3s1"}, "新しいバージョンがあります"),
                os_item or Item("os", "prod-node-1", Status.WARN, {"generated_at": "x", "upgradable_count": 3,
                                                          "security_fixable_count": 1, "reboot_required": True})),
        }

    def test_gauges_and_maintenance(self):
        h = node.render(self.snap(), {})
        for s in ("CPU 使用率", "メモリ使用率", "ディスク使用率", "91% (91.0 GB / 100.0 GB)", "85% を超えています",
                  "<svg", "v1.31.0+k3s1", "v1.32.3+k3s1", "新しいバージョンがあります", "再起動が必要", "未適用の更新"):
            self.assertIn(s, h)

    def test_three_nodes_rendered_per_node(self):
        snap = self.snap()
        extra = [Item(f"{k}:prod-node-{n}", f"prod-node-{n}", Status.OK, {"ratio": 10.0 * n, "used": 1.0, "total": 4.0})
                 for n in (2, 3) for k in ("cpu", "memory")]
        snap["node.resources"] = res("node.resources", *snap["node.resources"].items, *extra)
        h = node.render(snap, {})
        for n in (1, 2, 3):
            self.assertIn(f"CPU 使用率 (prod-node-{n})", h)
            self.assertIn(f"メモリ使用率 (prod-node-{n})", h)
        self.assertIn("ディスク使用率 (prod-node-1)", h)
        self.assertNotIn("ディスク使用率 (prod-node-2)", h)
        self.assertIn("再起動 (prod-node-1)", h)

    def test_stale_state_file(self):
        h = node.render(self.snap(os_item=Item("os", "os", Status.STALE, {}, "情報が古くなっています")), {})
        self.assertIn("情報が古い", h)
        self.assertNotIn("再起動が必要", h)

    def test_not_required_and_null_security(self):
        os_ = Item("os", "os", Status.OK, {"generated_at": "x", "upgradable_count": 0,
                                           "security_fixable_count": None, "reboot_required": False})
        self.assertIn("不要", node.render(self.snap(os_item=os_), {}))

    def test_error_and_collecting(self):
        h = node.render({"node.resources": STALE_ERR}, {})
        self.assertIn("取得失敗", h)
        self.assertIn("収集中", h)


class Cluster(unittest.TestCase):
    def test_all_blocks(self):
        snap = {"cluster.workloads": res(
            "cluster.workloads",
            Item("argocd/a", "app-a", Status.WARN, {"kind": "argocd", "sync": "OutOfSync", "health": "Progressing"}),
            Item("argocd/b", "app-b", Status.OK, {"kind": "argocd", "sync": "Synced", "health": "Healthy"}),
            Item("pod/n/p", "n/p", Status.CRIT, {"kind": "pod", "restarts": 7, "reason": "CrashLoopBackOff"}),
            Item("pod/n/q", "n/q", Status.WARN, {"kind": "pod", "restarts": 9, "reason": "OOMKilled"}),
            Item("cert/n/c", "n/c", Status.WARN, {"kind": "cert", "expires": "2026-06-10T00:00:00Z", "days_left": 9}, "有効期限まで 9 日"),
            Item("cert/n/d", "n/d", Status.CRIT, {"kind": "cert", "expires": None, "days_left": None}),
            Item("eso/n/e", "n/e", Status.CRIT, {"kind": "eso", "ready": False, "synced_at": None}),
            Item("store/s", "s", Status.OK, {"kind": "store", "ready": True, "synced_at": "2026-06-01T11:00:00Z"}))}
        h = cluster.render(snap, {})
        for s in ("app-a", "差分あり", "更新中", "同期済み", "正常", "n/p", "CrashLoopBackOff", "OOMKilled", "7",
                  "2026-06-10 09:00", "有効期限まで 9 日", "n/e", "同期できていません", "ClusterSecretStore",
                  "2026-06-01 20:00"):
            self.assertIn(s, h)

    def test_cards_do_not_inherit_other_kinds_status(self):
        snap = {"cluster.workloads": res(
            "cluster.workloads",
            Item("pod/n/p", "n/p", Status.CRIT, {"kind": "pod", "restarts": 7, "reason": "CrashLoopBackOff"}),
            Item("cert/n/c", "n/c", Status.OK, {"kind": "cert", "expires": "2026-12-10T00:00:00Z", "days_left": 60}))}
        cards = cluster.render(snap, {}).split("<article")
        cert = next(c for c in cards if "n/c" in c)
        pod = next(c for c in cards if "n/p" in c)
        self.assertIn("s-ok", cert.split(">")[0])
        self.assertIn("s-crit", pod.split(">")[0])

    def test_no_pods_is_empty_message(self):
        snap = {"cluster.workloads": res("cluster.workloads",
                                         Item("argocd/b", "app-b", Status.OK, {"kind": "argocd", "sync": "Synced", "health": "Healthy"}))}
        self.assertIn("該当するデータはありません", cluster.render(snap, {}))

    def test_unknown_values_do_not_crash(self):
        snap = {"cluster.workloads": res("cluster.workloads",
                                         Item("argocd/b", "app-b", Status.WARN, {"kind": "argocd", "sync": "Weird", "health": "Odd"}))}
        self.assertIn("Weird", cluster.render(snap, {}))

    def test_error(self):
        self.assertIn("取得失敗", cluster.render({"cluster.workloads": STALE_ERR}, {}))
        self.assertIn("収集中", cluster.render({}, {}))


class Data(unittest.TestCase):
    def test_cnpg_and_volsync(self):
        snap = {"cluster.data_protection": res(
            "cluster.data_protection",
            Item("cnpg/n/db", "n/db", Status.CRIT,
                 {"kind": "cnpg", "ready": True, "phase": "Cluster in healthy state", "wal_ok": False,
                  "last_backup": "2026-05-30T00:00:00Z"}, "最終バックアップから 2 日 経過しています"),
            Item("cnpg/n/db2", "n/db2", Status.OK,
                 {"kind": "cnpg", "ready": True, "phase": "ok", "wal_ok": True, "last_backup": None}),
            Item("volsync/n/mail", "n/mail", Status.OK,
                 {"kind": "volsync", "last_sync": "2026-06-01T10:00:00Z", "result": "Successful"}),
            Item("volsync/n/bad", "n/bad", Status.CRIT, {"kind": "volsync", "last_sync": None, "result": "Failed"}))}
        h = data.render(snap, {})
        for s in ("n/db", "失敗しています", "2026-05-30 09:00", "2 日 経過", "n/db2", "正常", "n/mail", "成功", "失敗",
                  "2026-06-01 19:00"):
            self.assertIn(s, h)

    def test_error_and_collecting(self):
        self.assertIn("取得失敗", data.render({"cluster.data_protection": STALE_ERR}, {}))
        self.assertIn("収集中", data.render({}, {}))


if __name__ == "__main__":
    unittest.main()
