import unittest
from datetime import datetime, timezone

import http_client
from helpers import FakeHttp, make_ctx
from model import Status
from sources import ci_github, connect_tailscale, connect_tunnel

LIST = "https://api.cloudflare.com/client/v4/accounts/ACC/cfd_tunnel"
CF = LIST + "/TID"


def cfg_with(**sources):
    import config
    c = make_ctx().config
    return config.Config(c.plans, c.servers, c.thresholds, c.credentials, c.exclude, sources)


class TunnelTest(unittest.TestCase):
    def ctx(self, status, conns):
        http = FakeHttp({
            LIST: {"success": True, "result": [{"id": "TID"}]},
            CF: {"success": True, "result": {"id": "TID", "status": status, "connections": []}},
            CF + "/connections": {"success": True, "result": conns}})
        cfg = cfg_with(**{"connect.tunnel": {"tunnel_name": "aramakisai-k3s"}})
        return make_ctx(cfg=cfg, http=http, env={"OPS_CLOUDFLARE_READ_TOKEN": "t", "TF_VAR_cloudflare_account_id": "ACC"}), http

    def test_healthy(self):
        ctx, http = self.ctx("healthy", [{"id": "a", "conns": [{"colo_name": "NRT"}, {"colo_name": "KIX"}]}])
        r = connect_tunnel.SOURCES[0].fetch(ctx)
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(r.items[0].values, {"state": "healthy", "connections": 2})
        self.assertEqual(http.calls[0]["bearer"], "t")
        self.assertEqual(http.calls[0]["params"]["name"], "aramakisai-k3s")

    def test_states(self):
        for state, st in (("degraded", Status.WARN), ("down", Status.CRIT), ("inactive", Status.WARN)):
            ctx, _ = self.ctx(state, [])
            self.assertEqual(connect_tunnel.SOURCES[0].fetch(ctx).status, st)

    def test_missing_token(self):
        ctx, _ = self.ctx("healthy", [])
        ctx = make_ctx(cfg=ctx.config, http=ctx.http)
        with self.assertRaises(Exception):
            connect_tunnel.SOURCES[0].fetch(ctx)


class TailscaleTest(unittest.TestCase):
    def test_devices(self):
        http = FakeHttp({
            "https://api.tailscale.com/api/v2/oauth/token": {"access_token": "AT", "token_type": "Bearer"},
            "https://api.tailscale.com/api/v2/tailnet/-/devices": {"devices": [
                {"hostname": "prod-node-1", "connectedToControl": True, "lastSeen": "2026-06-01T11:59:00Z",
                 "tags": ["tag:k3s"]},
                {"hostname": "dr-node", "connectedToControl": False, "lastSeen": "2026-05-01T00:00:00Z",
                 "tags": ["tag:k3s"]},
                {"hostname": "laptop", "connectedToControl": False, "lastSeen": "2026-05-30T00:00:00Z"}]}})
        cfg = cfg_with()
        ctx = make_ctx(cfg=cfg, http=http, env={"OPS_TAILSCALE_OAUTH_CLIENT_ID": "i", "OPS_TAILSCALE_OAUTH_CLIENT_SECRET": "s"})
        r = connect_tailscale.SOURCES[0].fetch(ctx)
        self.assertEqual([i.status for i in r.items], [Status.OK, Status.WARN, Status.OK])
        self.assertEqual(r.items[0].values["online"], 1)
        self.assertEqual(http.calls[0]["data"]["grant_type"], "client_credentials")
        self.assertEqual(http.calls[1]["bearer"], "AT")

    def test_token_failure(self):
        ctx = make_ctx(cfg=cfg_with(), http=FakeHttp({"https://api.tailscale.com/api/v2/oauth/token": http_client.HttpError("x", 401)}),
                       env={"OPS_TAILSCALE_OAUTH_CLIENT_ID": "i", "OPS_TAILSCALE_OAUTH_CLIENT_SECRET": "s"})
        with self.assertRaises(http_client.HttpError):
            connect_tailscale.SOURCES[0].fetch(ctx)


class GithubTest(unittest.TestCase):
    def ctx(self, runs, search):
        http = FakeHttp({
            "https://api.github.com/repos/o/r1/actions/runs": lambda **kw: {"workflow_runs": runs.get("r1", [])},
            "https://api.github.com/search/issues": lambda **kw: {"items": search(kw["params"]["q"])}})
        cfg = cfg_with(**{"ci.github": {"org": "o", "repos": ["r1"]}})
        return make_ctx(cfg=cfg, http=http, env={"OPS_GITHUB_TOKEN": "g"}), http

    def test_items(self):
        issue = lambda n, t: {"number": n, "title": t, "html_url": f"u{n}", "created_at": "2026-05-30T00:00:00Z",
                              "repository_url": "https://api.github.com/repos/o/r1"}

        def search(q):
            if "author:app/renovate" in q:
                return [issue(5, "Update x")]
            if "label:dr-incident" in q:
                return [issue(9, "DR")]
            return []
        runs = {"r1": [{"id": 1, "name": "CI", "html_url": "h", "created_at": "2026-05-31T00:00:00Z",
                        "updated_at": "2026-05-31T00:01:00Z", "head_branch": "main"}]}
        ctx, http = self.ctx(runs, search)
        r = ci_github.SOURCES[0].fetch(ctx)
        by = {i.key: i for i in r.items}
        self.assertEqual(by["run.r1.1"].status, Status.WARN)
        self.assertEqual(by["renovate.5"].status, Status.OK)
        self.assertEqual(by["incident.dr-incident.9"].status, Status.CRIT)
        self.assertEqual(r.status, Status.CRIT)
        self.assertEqual(http.calls[0]["params"]["created"], ">=2026-05-25")

    def test_clean_is_ok(self):
        ctx, _ = self.ctx({}, lambda q: [])
        self.assertEqual(ci_github.SOURCES[0].fetch(ctx).status, Status.OK)


if __name__ == "__main__":
    unittest.main()
