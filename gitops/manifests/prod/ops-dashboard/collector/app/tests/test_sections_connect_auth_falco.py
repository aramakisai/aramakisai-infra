import unittest
from datetime import timedelta

import falco_ingest
from helpers import FIXED_NOW, FakeHttp, make_ctx, temp_store
from model import Item, SourceResult, Status
from render.sections import auth, connect, falco
from sources import auth_zitadel, security_falco

AUTH_URL = "http://zitadel.zitadel.svc.cluster.local:8080/admin/v1/events/_search"


def res(sid, items, status=None, error=None):
    from model import make_result
    if error:
        return SourceResult(sid, Status.ERROR, FIXED_NOW, None, items, error)
    return make_result(sid, FIXED_NOW, items, status=status)


class Connect(unittest.TestCase):
    def snap(self, **over):
        s = {
            "connect.tunnel": res("connect.tunnel", [Item("tunnel", "tunnel", Status.WARN,
                                                          {"state": "degraded", "connections": 2})]),
            "connect.tailscale": res("connect.tailscale", [
                Item("device.node", "node", Status.OK, {"online": 1, "last_seen": "2026-06-01T11:59:00Z"}),
                Item("device.dr", "dr", Status.WARN, {"online": 0, "last_seen": "2026-05-01T00:00:00Z"})]),
            "ci.github": res("ci.github", [
                Item("run.infra.1", "k3s-upgrade", Status.WARN, {"repo": "infra", "failed_at": "2026-06-01T01:00:00Z",
                                                                  "url": "https://github.com/o/infra/actions/runs/1"}),
                Item("renovate.5", "chore(deps): bump x", Status.OK, {"repo": "infra", "opened_at": "2026-05-30T00:00:00Z",
                                                                       "url": "https://github.com/o/infra/pull/5"}),
                Item("incident.dr-incident.9", "DR 発生", Status.CRIT, {"label": "dr-incident", "repo": "infra",
                                                                       "opened_at": "2026-06-01T00:00:00Z",
                                                                       "url": "javascript:alert(1)"})], status=None),
        }
        s.update(over)
        return s

    def test_values(self):
        h = connect.render(self.snap(), {})
        for s in ("一部の接続が切断", "接続数: 2", "オフライン", "dr", "k3s-upgrade", "実行結果を開く",
                  "chore(deps): bump x", "DR 検知 (dr-incident)", "DR 発生", "2026-06-01 20:59"):
            self.assertIn(s, h)
        self.assertIn('href="https://github.com/o/infra/actions/runs/1"', h)
        self.assertNotIn("javascript:", h)

    def test_empty_ci(self):
        h = connect.render(self.snap(**{"ci.github": res("ci.github", [], status=Status.OK)}), {})
        self.assertIn("対応中のインシデントはありません", h)

    def test_error_and_collecting(self):
        err = res("connect.tunnel", [], error="boom")
        h = connect.render(self.snap(**{"connect.tunnel": err, "ci.github": res("ci.github", [], error="gh down")}), {})
        self.assertIn("boom", h)
        self.assertIn("gh down", h)
        self.assertIn("収集中", connect.render({}, {}))


class AuthFalcoBase(unittest.TestCase):
    def setUp(self):
        self.d, self.st = temp_store()

    def tearDown(self):
        self.st.close()
        self.d.cleanup()


class Auth(AuthFalcoBase):
    def snap(self):
        evs = [{"sequence": "1", "creationDate": "2026-06-01T10:00:00Z", "type": {"type": "user.human.password.check.failed"},
                "aggregate": {"id": "u1"}, "payload": {"loginName": "alice@x"}},
               {"sequence": "2", "creationDate": "2026-05-20T10:00:00Z", "type": {"type": "user.locked"},
                "aggregate": {"id": "u2"}}]
        ctx = make_ctx(http=FakeHttp({AUTH_URL: {"events": evs}}), store=self.st, env={"OPS_ZITADEL_READER_PAT": "p"})
        return {"auth.zitadel": auth_zitadel.SOURCES[0].fetch(ctx)}

    def test_ranges(self):
        s = self.snap()
        h24 = auth.render(s, {})
        self.assertIn("06-01T19", h24)
        self.assertNotIn("05-20", h24.split("直近の認証失敗")[0])
        self.assertIn("alice@x", h24)
        self.assertIn("パスワード誤り", h24)
        self.assertIn("数値を表で見る", h24)
        h30 = auth.render(s, {"auth": ["30d"]})
        self.assertIn("05-20", h30)
        self.assertNotIn("06-01T19", h30)
        self.assertIn('class="cur"', h30)

    def test_error_and_empty(self):
        self.assertIn("収集中", auth.render({}, {}))
        h = auth.render({"auth.zitadel": res("auth.zitadel", [], error="x401")}, {})
        self.assertIn("x401", h)


class Falco(AuthFalcoBase):
    def snap(self, failed=0):
        ctx = make_ctx(store=self.st)
        add = lambda rule, prio, m: self.st.insert_falco_event(
            {"rule": rule, "priority": prio, "output": "out " + rule, "time": "t",
             "output_fields": {"k8s.ns.name": "ns", "k8s.pod.name": "p"}}, FIXED_NOW - timedelta(minutes=m))
        add("RuleA", "Warning", 10)
        add("RuleB", "Notice", 60 * 30)
        falco_ingest._failures = failed
        try:
            return {"security.falco": security_falco.SOURCES[0].render(ctx, {"falco": ["7d"]})}
        finally:
            falco_ingest._failures = 0

    def test_render(self):
        h = falco.render(self.snap(), {"falco": ["7d"]})
        for s in ("RuleA", "RuleB", "ns/p", "警告 (Warning)", "検知は 90 日間保存します", "ルール別の検知件数", "数値を表で見る"):
            self.assertIn(s, h)
        self.assertNotIn("保存に失敗", h)
        self.assertIn('class="cur"', h)

    def test_ingest_failed_note(self):
        self.assertIn("3 件)。Discord への通知は継続しています", falco.render(self.snap(failed=3), {}))

    def test_empty_period(self):
        ctx = make_ctx(store=self.st)
        s = {"security.falco": security_falco.SOURCES[0].render(ctx, {})}
        self.assertIn("この期間の検知はありません", falco.render(s, {}))

    def test_error_and_collecting(self):
        self.assertIn("収集中", falco.render({}, {}))
        self.assertIn("e1", falco.render({"security.falco": res("security.falco", [], error="e1")}, {}))


if __name__ == "__main__":
    unittest.main()
