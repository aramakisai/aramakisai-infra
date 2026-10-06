import unittest
from datetime import timedelta

from helpers import FIXED_NOW, FakeK8s, make_ctx
from model import Status, iso
from sources import cluster_data_protection as dp
from sources import cluster_workloads as wl

AGO = lambda **kw: iso(FIXED_NOW - timedelta(**kw))
AHEAD = lambda **kw: iso(FIXED_NOW + timedelta(**kw))
OK = {"conditions": [{"type": "Ready", "status": "True"}]}
BAD = {"conditions": [{"type": "Ready", "status": "False"}]}


def meta(name, ns=None):
    return {"name": name, **({"namespace": ns} if ns else {})}


def app(name, sync="Synced", health="Healthy", dest="prod"):
    return {"metadata": meta(name, "argocd"), "spec": {"destination": {"namespace": dest}},
            "status": {"sync": {"status": sync}, "health": {"status": health}}}


def pod(name, ns="prod", restarts=0, waiting=None, last_term=None):
    cs = {"restartCount": restarts, "state": {"waiting": {"reason": waiting}} if waiting else {"running": {}}}
    if last_term:
        cs["lastState"] = {"terminated": last_term}
    return {"metadata": meta(name, ns), "status": {"phase": "Running", "containerStatuses": [cs]}}


def workload_k8s(**over):
    paths = {
        "/apis/argoproj.io/v1alpha1/applications": {"items": [
            app("cms"), app("authentik", sync="OutOfSync"), app("bad", health="Degraded"),
            app("vaultwarden"), app("rp", dest="room-presence")]},
        "/api/v1/pods": {"items": [
            pod("ok"), pod("loop", waiting="CrashLoopBackOff", restarts=9), pod("many", restarts=6),
            pod("oom", restarts=1, last_term={"reason": "OOMKilled", "finishedAt": AGO(hours=1)}),
            pod("oldoom", restarts=1, last_term={"reason": "OOMKilled", "finishedAt": AGO(days=3)}),
            pod("frozen", ns="room-presence", restarts=50)]},
        "/apis/cert-manager.io/v1/certificates": {"items": [
            {"metadata": meta("a", "prod"), "status": {"notAfter": AHEAD(days=60, hours=1), **OK}},
            {"metadata": meta("b", "prod"), "status": {"notAfter": AHEAD(days=10, hours=1), **OK}},
            {"metadata": meta("c", "prod"), "status": {"notAfter": AHEAD(days=3, hours=1), **OK}}]},
        "/apis/external-secrets.io/v1/externalsecrets": {"items": [
            {"metadata": meta("s1", "prod"), "status": {**OK, "refreshTime": AGO(minutes=5)}},
            {"metadata": meta("s2", "prod"), "status": BAD}]},
        "/apis/external-secrets.io/v1/clustersecretstores": {"items": [
            {"metadata": meta("infisical"), "status": OK}]},
    }
    paths.update(over)
    return FakeK8s(paths)


class WorkloadsTest(unittest.TestCase):
    def setUp(self):
        res = wl.SOURCES[0].fetch(make_ctx(k8s=workload_k8s()))
        self.by = {i.key: i for i in res.items}
        self.res = res

    def test_argocd(self):
        self.assertEqual(self.by["argocd/cms"].status, Status.OK)
        self.assertEqual(self.by["argocd/authentik"].status, Status.WARN)
        self.assertEqual(self.by["argocd/bad"].status, Status.CRIT)

    def test_frozen_excluded(self):
        self.assertNotIn("argocd/vaultwarden", self.by)
        self.assertNotIn("argocd/rp", self.by)
        self.assertNotIn("pod/room-presence/frozen", self.by)

    def test_pods(self):
        self.assertNotIn("pod/prod/ok", self.by)
        self.assertNotIn("pod/prod/oldoom", self.by)
        self.assertEqual(self.by["pod/prod/loop"].values["reason"], "CrashLoopBackOff")
        self.assertEqual(self.by["pod/prod/loop"].status, Status.CRIT)
        self.assertEqual(self.by["pod/prod/oom"].values["reason"], "OOMKilled")
        self.assertEqual(self.by["pod/prod/many"].values["restarts"], 6)

    def test_certs(self):
        self.assertEqual([self.by[f"cert/prod/{n}"].status for n in "abc"], [Status.OK, Status.WARN, Status.CRIT])
        self.assertEqual(self.by["cert/prod/b"].values["days_left"], 10)

    def test_eso(self):
        self.assertEqual(self.by["eso/prod/s1"].status, Status.OK)
        self.assertEqual(self.by["eso/prod/s2"].status, Status.CRIT)
        self.assertEqual(self.by["store/infisical"].status, Status.OK)
        self.assertEqual(self.res.status, Status.CRIT)


def cluster(name, ns, ready=True, archiving=True):
    conds = [{"type": "Ready", "status": str(ready)}]
    if archiving is not None:
        conds.append({"type": "ContinuousArchiving", "status": str(archiving)})
    return {"metadata": meta(name, ns), "status": {"phase": "Cluster in healthy state", "conditions": conds}}


def backup(cluster_name, ns, stopped, phase="completed"):
    return {"metadata": meta(f"{cluster_name}-{stopped}", ns), "spec": {"cluster": {"name": cluster_name}},
            "status": {"phase": phase, "stoppedAt": stopped}}


def volsync(name, last, result="Successful"):
    return {"metadata": meta(name, "mailserver"), "status": {
        "lastSyncTime": last, "latestMoverStatus": {"result": result}}}


class DataProtectionTest(unittest.TestCase):
    def fetch(self, clusters, backups, repl):
        base = "/apis/postgresql.cnpg.io/v1"
        k8s = FakeK8s({f"{base}/clusters": {"items": clusters}, f"{base}/backups": {"items": backups},
                       "/apis/volsync.backube/v1alpha1/replicationsources": {"items": repl}})
        return dp.SOURCES[0].fetch(make_ctx(k8s=k8s))

    def test_healthy(self):
        last = AGO(hours=10)
        res = self.fetch([cluster("cms-db", "prod")],
                         [backup("cms-db", "prod", AGO(days=2)), backup("cms-db", "prod", last),
                          backup("cms-db", "prod", AGO(hours=1), phase="failed")],
                         [volsync("mail", AGO(hours=2))])
        by = {i.key: i for i in res.items}
        self.assertEqual(res.status, Status.OK)
        self.assertEqual(by["cnpg/prod/cms-db"].values["last_backup"], last)
        self.assertIs(by["cnpg/prod/cms-db"].values["wal_ok"], True)
        self.assertEqual(by["volsync/mailserver/mail"].values["result"], "Successful")

    def test_wal_failing(self):
        res = self.fetch([cluster("cms-db", "prod", archiving=False)], [backup("cms-db", "prod", AGO(hours=1))], [])
        self.assertEqual(res.items[0].status, Status.CRIT)
        self.assertIs(res.items[0].values["wal_ok"], False)

    def test_backup_too_old(self):
        res = self.fetch([cluster("cms-db", "prod")], [backup("cms-db", "prod", AGO(hours=40))], [])
        self.assertEqual(res.items[0].status, Status.CRIT)
        self.assertIn("40 時間", res.items[0].note)

    def test_no_backup_with_max_age_is_crit(self):
        res = self.fetch([cluster("cms-db", "prod")], [], [])
        self.assertEqual(res.items[0].status, Status.CRIT)

    def test_cluster_without_declared_max_age_not_checked(self):
        res = self.fetch([cluster("other", "prod")], [], [])
        self.assertEqual(res.items[0].status, Status.OK)

    def test_frozen_cluster_excluded(self):
        res = self.fetch([cluster("db", "vaultwarden", ready=False)], [], [])
        self.assertEqual(list(res.items), [])
        self.assertEqual(res.status, Status.EMPTY)

    def test_volsync(self):
        res = self.fetch([], [], [volsync("old", AGO(hours=13)), volsync("fail", AGO(hours=1), "Failed")])
        self.assertEqual([i.status for i in res.items], [Status.CRIT, Status.CRIT])


if __name__ == "__main__":
    unittest.main()
