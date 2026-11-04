import dataclasses
import unittest
from datetime import timedelta

import config
from helpers import FIXED_NOW, FakeK8s, make_ctx
from http_client import HttpError
from model import Status, iso
from sources import festival_workloads as fw
from test_festival_config import raw_festival

PODS = "/api/v1/namespaces/{}/pods"
PMET = "/apis/metrics.k8s.io/v1beta1/namespaces/{}/pods"
CLUSTER = "/apis/postgresql.cnpg.io/v1/namespaces/prod/clusters/directus-db"
DEPLOY = "/apis/apps/v1/namespaces/prod/deployments/cms"
DB_SEL = "cnpg.io/cluster=directus-db,cnpg.io/podRole=instance"
RECENT = iso(FIXED_NOW - timedelta(minutes=10))
OLD = iso(FIXED_NOW - timedelta(minutes=45))


def pod(name, ns="prod", ready=True, ip="10.1.0.1", host="10.0.1.1", cpu_limit="1", mem_limit=None, restarts=(),
        waiting=None, term=None, containers=("app",)):
    """term は (reason, finishedAt)。restarts は finishedAt の一覧 (lastState.terminated)。"""
    cs = []
    for c in containers:
        st = {"name": c, "state": {}, "restartCount": len(restarts)}
        if restarts:
            st["lastState"] = {"terminated": {"reason": "Error", "finishedAt": restarts[0]}}
        if term:
            st["lastState"] = {"terminated": {"reason": term[0], "finishedAt": term[1]}}
        if waiting:
            st["state"] = {"waiting": {"reason": waiting}}
        cs.append(st)
    res = {"limits": {k: v for k, v in (("cpu", cpu_limit), ("memory", mem_limit)) if v}}
    return {"metadata": {"name": name, "namespace": ns},
            "spec": {"containers": [{"name": c, "resources": res} for c in containers]},
            "status": {"phase": "Running", "podIP": ip, "hostIP": host, "containerStatuses": cs,
                       "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


def pm(name, ns="prod", **cpu):
    return {"metadata": {"name": name, "namespace": ns},
            "containers": [{"name": c, "usage": {"cpu": v, "memory": "1Mi"}} for c, v in cpu.items()]}


def paths(cms_pods, db_pods=(), cf_pods=(), metrics=(), cf_metrics=(), primary="db-1", replicas=2):
    return {DEPLOY: {"spec": {"replicas": replicas}},
            CLUSTER: {"status": {"currentPrimary": primary}},
            (PODS.format("prod"), "app=cms"): {"items": list(cms_pods)},
            (PODS.format("prod"), DB_SEL): {"items": list(db_pods)},
            (PODS.format("cloudflared"), "app=cloudflared"): {"items": list(cf_pods)},
            (PMET.format("prod"), "app=cms"): {"items": [m for m in metrics if m["metadata"]["name"].startswith("cms")]},
            (PMET.format("prod"), DB_SEL): {"items": [m for m in metrics if m["metadata"]["name"].startswith("db")]},
            (PMET.format("cloudflared"), "app=cloudflared"): {"items": list(cf_metrics)}}


class RecordingK8s(FakeK8s):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.params = {}
        self.texts = {}

    def kubelet_text(self, ip, path):
        r = self.texts.get((ip, path), HttpError("no route"))
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, path, params=None, **kw):
        sel = (params or {}).get("labelSelector")
        self.params[path] = self.params.get(path, []) + [sel]
        return self.paths[(path, sel)] if (path, sel) in self.paths else super().get(path)


def run(p, kubelet=None, texts=None, k8s=None):
    k8s = k8s or RecordingK8s(p, kubelet=kubelet)
    k8s.texts.update(texts or {})
    r = fw.SOURCES[0].fetch(make_ctx(k8s=k8s))
    return {i.key: i for i in r.items}, k8s


def cadv(*rows):
    """rows は (namespace, pod, container, throttled, periods)。"""
    out = []
    for ns, pd, c, t, per in rows:
        lab = f'container="{c}",namespace="{ns}",pod="{pd}"'
        out += [f"container_cpu_cfs_throttled_periods_total{{{lab}}} {t}", f"container_cpu_cfs_periods_total{{{lab}}} {per}"]
    return "\n".join(out) + "\n"


def summary(ns, name, **rss):
    return {"pods": [{"podRef": {"namespace": ns, "name": name},
                      "containers": [{"name": c, "memory": {"rssBytes": v, "workingSetBytes": 10**12}}
                                     for c, v in rss.items()]}]}


CADV = ("10.0.1.1", "/metrics/cadvisor")


class WorkloadsTest(unittest.TestCase):
    def setUp(self):
        fw._prev_cfs = {}

    def test_cms_item(self):
        p = paths([pod("cms-a", cpu_limit="1", restarts=(RECENT,)),
                   pod("cms-b", ready=False, cpu_limit="500m", restarts=(OLD,), term=("OOMKilled", RECENT), waiting="CrashLoopBackOff"),
                   pod("cms-c", cpu_limit=None)],
                  metrics=[pm("cms-a", app="250m"), pm("cms-b", app="400m"), pm("cms-c", app="2")], replicas=3)
        items, k8s = run(p)
        v = items["cms"].values
        self.assertEqual((v["replicas"], v["ready"]), (3, 2))
        self.assertEqual(v["restarts_recent"], 2)  # cms-a と cms-b (OOM の終了時刻が窓内)
        self.assertTrue(v["oom_recent"])
        self.assertTrue(v["crashloop"])
        self.assertEqual(v["cpu_ratio_max"], 0.8)  # limit なしの pod は除く
        self.assertEqual(items["cms"].status, Status.OK)
        self.assertEqual(k8s.params[PODS.format("prod")], ["app=cms", DB_SEL])
        self.assertEqual(k8s.params[PODS.format("cloudflared")], ["app=cloudflared"])

    def test_cms_old_events_and_missing_metrics(self):
        p = paths([pod("cms-a", restarts=(OLD,), term=("OOMKilled", OLD))], metrics=[])
        v = run(p)[0]["cms"].values
        self.assertEqual((v["restarts_recent"], v["oom_recent"], v["crashloop"]), (0, False, False))
        self.assertIsNone(v["cpu_ratio_max"])

    def test_db_items_and_endpoints(self):
        db = [pod("db-1", ip="10.1.0.1", cpu_limit="2", mem_limit="1Gi"),
              pod("db-2", ip="10.1.0.2", cpu_limit="2", term=("OOMKilled", RECENT), restarts=(RECENT,))]
        p = paths([], db_pods=db, metrics=[pm("db-1", app="1"), pm("db-2", app="200m")])
        items, k8s = run(p)
        self.assertEqual(items["db.db-1"].values["role"], "primary")
        self.assertEqual(items["db.db-1"].values["cpu_ratio"], 0.5)
        self.assertEqual(items["db.db-2"].values["role"], "replica")
        self.assertEqual(items["db.db-2"].values["cpu_ratio"], 0.1)
        self.assertTrue(items["db.db-2"].values["oom_recent"])
        self.assertEqual(items["db.db-2"].values["restarts_recent"], 1)
        self.assertEqual(fw.db_endpoints(), (fw.DbEndpoint("db-1", "10.1.0.1", "primary"),
                                             fw.DbEndpoint("db-2", "10.1.0.2", "replica")))
        self.assertNotIn("10.1.0", repr([i.values for i in items.values()]))

    def test_cloudflared_item(self):
        cf = [pod("cf-a", ns="cloudflared", cpu_limit="1"), pod("cf-b", ns="cloudflared", cpu_limit="500m")]
        p = paths([], cf_pods=cf, cf_metrics=[pm("cf-a", ns="cloudflared", app="100m"),
                                              pm("cf-b", ns="cloudflared", app="300m")])
        v = run(p)[0]["cloudflared"].values
        self.assertEqual((v["pods"], v["cpu_ratio_max"]), (2, 0.6))

    def test_db_memory_rss_ratio(self):
        db = [pod("db-1", mem_limit="1000"), pod("db-2", host="10.0.1.2", mem_limit="2000"), pod("db-3", host="10.0.1.9", mem_limit="1000")]
        kubelet = {"10.0.1.1": summary("prod", "db-1", app=250),
                   "10.0.1.2": summary("prod", "db-2", app=1500), "10.0.1.9": HttpError("down")}
        items, _ = run(paths([], db_pods=db), kubelet=kubelet)
        self.assertEqual(items["db.db-1"].values["memory_rss_ratio"], 0.25)
        self.assertEqual(items["db.db-2"].values["memory_rss_ratio"], 0.75)
        self.assertIsNone(items["db.db-3"].values["memory_rss_ratio"])  # そのノードの Pod だけ欠かす
        self.assertIsNotNone(items["db.db-3"].note)
        self.assertEqual(items["db.db-3"].values["role"], "replica")

    def test_throttle_ratio_needs_previous_sample(self):
        p = paths([pod("cms-a", containers=("app", "side"))], cf_pods=[pod("cf-a", ns="cloudflared")])
        k8s = RecordingK8s(p)
        first = cadv(("prod", "cms-a", "app", 10, 100), ("prod", "cms-a", "side", 0, 100), ("cloudflared", "cf-a", "app", 5, 50))
        items, _ = run(p, texts={CADV: first}, k8s=k8s)
        self.assertIsNone(items["cms"].values["throttle_ratio_max"])  # 初回は計算から除かれる
        self.assertIsNotNone(items["cms"].note)
        second = cadv(("prod", "cms-a", "app", 40, 200), ("prod", "cms-a", "side", 0, 200),
                      ("cloudflared", "cf-a", "app", 5, 50), ("prod", "cms-a", "", 999, 1000))
        items, _ = run(p, texts={CADV: second}, k8s=k8s)
        self.assertEqual(items["cms"].values["throttle_ratio_max"], 0.3)
        self.assertEqual(items["cloudflared"].values["throttle_ratio_max"], 0.0)  # 期間の増分 0
        self.assertIsNone(items["cms"].note)
        third = cadv(("prod", "cms-a", "app", 10, 300), ("prod", "cms-a", "side", 50, 300))  # app はカウンタの減少
        items, _ = run(p, texts={CADV: third}, k8s=k8s)
        self.assertEqual(items["cms"].values["throttle_ratio_max"], 0.5)
        self.assertIsNone(items["cloudflared"].values["throttle_ratio_max"])  # 行が無い

    def test_throttle_new_pod_is_skipped_and_kubelet_down_only_loses_that_node(self):
        p = paths([pod("cms-a"), pod("cms-b", host="10.0.1.2")])
        k8s = RecordingK8s(p)
        run(p, texts={CADV: cadv(("prod", "cms-a", "app", 0, 100))}, k8s=k8s)
        second = cadv(("prod", "cms-a", "app", 10, 200), ("prod", "cms-b", "app", 90, 100))
        items, _ = run(p, texts={CADV: second}, k8s=k8s)  # 10.0.1.2 は届かない
        self.assertEqual(items["cms"].values["throttle_ratio_max"], 0.1)
        p2 = paths([pod("cms-a")])
        k8s2 = RecordingK8s(p2)
        run(p2, texts={CADV: cadv(("prod", "cms-a", "app", 0, 100))}, k8s=k8s2)
        run(p2, texts={CADV: cadv(("prod", "cms-a", "app", 10, 200), ("prod", "cms-new", "app", 90, 100))}, k8s=k8s2)

    def test_undeclared_or_disabled_is_empty_without_requests(self):
        cfg = make_ctx().config
        raw = raw_festival()
        raw["disabled_sources"] = ["festival.workloads"]
        for c in (dataclasses.replace(cfg, festival=None),
                  dataclasses.replace(cfg, festival=config.parse_festival(raw))):
            r = fw.SOURCES[0].fetch(make_ctx(cfg=c, k8s=FakeK8s()))
            self.assertEqual((r.status, list(r.items)), (Status.EMPTY, []))

    def test_interval(self):
        self.assertEqual(fw.SOURCES[0].interval, timedelta(seconds=30))


if __name__ == "__main__":
    unittest.main()
