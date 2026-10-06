import re
import unittest
from datetime import date, datetime, timezone

import config
from helpers import FakeHttp, make_ctx
from model import Status
from sources import billing_cloudflare, billing_github, billing_hcp_terraform, billing_hetzner, billing_hetzner_os
from sources import billing_infisical, billing_netdata, billing_tailscale
from plan import credential_items, hetzner_estimate, plan_item, server_diff, usage_status

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
TOML = """
[[plans]]
service = "cloudflare_workers"
name = "Workers Free"
paid = false
from = "2020-01-01"
until = "2026-10-31"
limits = { requests_day = 100_000 }
[[plans]]
service = "cloudflare_workers"
name = "Workers Paid"
paid = true
from = "2026-11-01"
until = "2026-11-30"
limits = { requests_month = 10_000_000 }
[[plans]]
service = "cloudflare_workers"
name = "Workers Free"
paid = false
from = "2026-12-01"
limits = { requests_day = 100_000 }
[[plans]]
service = "cloudflare_r2"
name = "R2 Free"
paid = false
from = "2020-01-01"
limits = { storage_gb = 10, class_a_month = 1_000_000, class_b_month = 10_000_000 }
[[plans]]
service = "cloudflare_zero_trust"
name = "ZT"
paid = false
from = "2020-01-01"
limits = { users = 50 }
[[plans]]
service = "hcp_terraform"
name = "Free"
paid = false
from = "2020-01-01"
limits = { managed_resources = 500 }
[[plans]]
service = "infisical"
name = "Infisical Free"
paid = false
from = "2020-01-01"
limits = { identities = 5 }
[[plans]]
service = "tailscale"
name = "TS"
paid = false
from = "2020-01-01"
limits = { users = 3, devices = 100 }
[[plans]]
service = "netdata_cloud"
name = "ND"
paid = false
from = "2020-01-01"
limits = { nodes = 5 }
[[plans]]
service = "github"
name = "GH"
paid = false
from = "2020-01-01"
limits = { actions_minutes_month = 2000, packages_storage_gb = 0.5 }
[[plans]]
service = "hetzner_object_storage"
name = "OS"
paid = true
from = "2020-01-01"
limits = { storage_gb = 1000, price_month = 5 }
[[servers]]
name = "prod-node-1"
[[servers]]
name = "event-node"
until = "2026-05-31"
[[credentials]]
name = "A"
expires_on = "2026-06-20"
[[credentials]]
name = "B"
expires_on = "2026-05-31"
[[credentials]]
name = "C"
expires_on = "2027-01-01"
[sources."billing.github"]
org = "o"
[sources."billing.hcp_terraform"]
org = "o"
[sources."billing.cloudflare"]
account_id = "acc"
[sources."billing.hetzner_os"]
endpoint = "https://fsn1.your-objectstorage.com"
buckets = ["b1", "b2"]
"""
CFG = config.parse(TOML)


def at(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=timezone.utc)


class Plan(unittest.TestCase):
    def test_period_switch(self):
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 10, 31)).values["plan"], "Workers Free")
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 11, 1)).values["plan"], "Workers Paid")
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 12, 1)).values["plan"], "Workers Free")

    def test_jst_boundary(self):
        # 10/31 15:00 UTC は JST で 11/1 00:00
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 10, 31, 15)).values["plan"], "Workers Paid")

    def test_undeclared_warns(self):
        self.assertEqual(plan_item(CFG, "uptimerobot", NOW).status, Status.WARN)

    def test_revert_forgotten(self):
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 12, 5), actual_paid=True).status, Status.CRIT)
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 12, 5), actual_paid=False).status, Status.OK)
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 12, 5)).status, Status.OK)
        # 期間内は有料が正しい
        self.assertEqual(plan_item(CFG, "cloudflare_workers", at(2026, 11, 10), actual_paid=True).status, Status.OK)

    def test_revert_declared_paid_continues(self):
        cfg = config.parse(TOML.replace('until = "2026-11-30"', 'until = "2026-11-30"').replace(
            'name = "Workers Free"\npaid = false\nfrom = "2026-12-01"',
            'name = "Workers Paid2"\npaid = true\nfrom = "2026-12-01"'))
        it = plan_item(cfg, "cloudflare_workers", at(2026, 12, 5))
        self.assertEqual(it.status, Status.CRIT)

    def test_usage_thresholds(self):
        self.assertEqual(usage_status(79, 100, 0.8)[0], Status.OK)
        self.assertEqual(usage_status(80, 100, 0.8)[0], Status.WARN)
        self.assertEqual(usage_status(100, 100, 0.8)[0], Status.CRIT)
        self.assertEqual(usage_status(None, 100, 0.8)[0], Status.OK)

    def test_credentials(self):
        st = {i.key: i.status for i in credential_items(CFG, NOW)}
        self.assertEqual(st, {"cred.A": Status.WARN, "cred.B": Status.CRIT, "cred.C": Status.OK})

    def test_server_diff(self):
        sv = [{"name": "prod-node-1", "status": "running"}, {"name": "tmp", "status": "running"}]
        self.assertEqual(server_diff(CFG, sv, NOW), (["tmp"], []))
        self.assertEqual(server_diff(CFG, [], at(2026, 5, 31)), ([], ["event-node", "prod-node-1"]))
        self.assertEqual(server_diff(CFG, [{"name": "prod-node-1", "status": "off"}], NOW), ([], ["prod-node-1"]))


def server(name="prod-node-1", out=0, inc=20e12, created="2026-05-01T00:00:00+00:00", status="running"):
    return {"name": name, "status": status, "created": created, "server_type": {"name": "cx22"},
            "location": {"name": "fsn1"}, "outgoing_traffic": out, "included_traffic": inc}


PRICES = {("cx22", "fsn1"): {"price_hourly": {"net": "0.0100"}, "price_monthly": {"net": "5.0000"},
                              "price_per_tb_traffic": {"net": "1.0000"}}}


class Hetzner(unittest.TestCase):
    def test_estimate_caps_at_monthly_and_traffic(self):
        e = hetzner_estimate([server(out=22e12)], PRICES, at(2026, 6, 30), 5.0)
        self.assertEqual(e["total"], 12.0)  # 5 (月額頭打ち) + 2TB 超過 + OS 5
        e = hetzner_estimate([server(created="2026-06-01T06:00:00+00:00")], PRICES, NOW)
        self.assertEqual(e["total"], 0.06)  # 6 時間分

    def fetch(self, servers):
        http = FakeHttp({
            "https://api.hetzner.cloud/v1/servers": {"servers": servers, "meta": {"pagination": {"next_page": None}}},
            "https://api.hetzner.cloud/v1/pricing": {"pricing": {"server_types": [
                {"name": "cx22", "prices": [{"location": "fsn1", **PRICES[("cx22", "fsn1")]}]}]}}})
        ctx = make_ctx(CFG, http=http, env={"OPS_HCLOUD_READ_TOKEN": "t"}, now=NOW)
        return billing_hetzner.SOURCES[0].fetch(ctx), http

    def test_ok(self):
        r, http = self.fetch([server()])
        self.assertEqual(r.status, Status.OK)
        self.assertEqual(http.calls[0]["bearer"], "t")
        self.assertEqual({i.key for i in r.items}, {"hetzner.server.prod-node-1", "hetzner.estimate"})

    def test_unexpected_and_missing(self):
        r, _ = self.fetch([server("tmp")])
        by = {i.key: i for i in r.items}
        self.assertEqual(r.status, Status.CRIT)
        self.assertEqual(by["hetzner.server.tmp"].status, Status.CRIT)
        self.assertEqual(by["hetzner.server.prod-node-1"].values["state"], "absent")

    def test_missing_secret_raises(self):
        with self.assertRaises(Exception):
            billing_hetzner.SOURCES[0].fetch(make_ctx(CFG, env={}))


def gql(day=1000, month=5000, storage=(4e9, 1e9), ops=()):
    return {"data": {"viewer": {"accounts": [{
        "day": [{"sum": {"requests": day}}], "month": [{"sum": {"requests": month}}],
        "storage": [{"max": {"payloadSize": storage[0], "metadataSize": storage[1]},
                     "dimensions": {"datetime": "2026-06-01T11:00:00Z", "bucketName": "b"}}],
        "ops": [{"sum": {"requests": n}, "dimensions": {"actionType": a}} for a, n in ops]}]}}}


class Cloudflare(unittest.TestCase):
    def run_cf(self, now, gq, subs=(), users=7):
        http = FakeHttp({
            "https://api.cloudflare.com/client/v4/accounts/acc/subscriptions": {"result": list(subs)},
            "https://api.cloudflare.com/client/v4/graphql": gq,
            "https://api.cloudflare.com/client/v4/accounts/acc/access/users": {
                "result": [], "result_info": {"total_count": users}}})
        ctx = make_ctx(CFG, http=http, env={"OPS_CLOUDFLARE_READ_TOKEN": "t", "TF_VAR_cloudflare_account_id": "acc"}, now=now)
        r = billing_cloudflare.SOURCES[0].fetch(ctx)
        return {i.key: i for i in r.items}, r

    def test_free_daily(self):
        by, r = self.run_cf(NOW, gql(day=90_000, ops=[("PutObject", 10), ("GetObject", 20), ("DeleteObject", 99)]))
        self.assertIn("quota.workers_day", by)
        self.assertNotIn("quota.workers_month", by)
        self.assertEqual(by["quota.workers_day"].status, Status.WARN)
        self.assertEqual(by["quota.r2_class_a"].values["used"], 10)
        self.assertEqual(by["quota.r2_class_b"].values["used"], 20)
        self.assertEqual(by["quota.r2_storage"].values["used"], 5.0)
        self.assertEqual(by["quota.zt_users"].values["used"], 7)
        self.assertEqual(r.status, Status.WARN)

    def test_storage_sums_latest_per_bucket(self):
        g = gql()
        g["data"]["viewer"]["accounts"][0]["storage"] = [
            {"max": {"payloadSize": 3e9, "metadataSize": 0}, "dimensions": {"datetime": "2026-06-01T11:00:00Z", "bucketName": "a"}},
            {"max": {"payloadSize": 1e9, "metadataSize": 1e9}, "dimensions": {"datetime": "2026-06-01T10:00:00Z", "bucketName": "b"}},
            {"max": {"payloadSize": 9e9, "metadataSize": 0}, "dimensions": {"datetime": "2026-06-01T09:00:00Z", "bucketName": "a"}}]
        by, _ = self.run_cf(NOW, g)
        self.assertEqual(by["quota.r2_storage"].values["used"], 5.0)

    def test_storage_query_orders_by_a_dimension(self):
        # GraphQL Analytics は orderBy に dimensions へ含めていないフィールドを指定すると拒否する
        q = re.search(r"r2StorageAdaptiveGroups\(.*", billing_cloudflare.QUERY).group(0)
        self.assertIn("orderBy: [datetime_DESC]", q)
        self.assertRegex(q, r"dimensions \{[^}]*\bdatetime\b")

    def test_paid_monthly_and_billed(self):
        sub = {"state": "Paid", "price": 5, "frequency": "monthly", "rate_plan": {"public_name": "Workers Paid"}}
        by, _ = self.run_cf(at(2026, 11, 10), gql(month=11_000_000), subs=[sub])
        self.assertEqual(by["quota.workers_month"].status, Status.CRIT)
        self.assertEqual(by["billed.cloudflare"].values["amount"], 5)
        self.assertEqual(by["plan.cloudflare_workers"].status, Status.OK)

    def test_forgotten_revert(self):
        sub = {"state": "Paid", "price": 5, "frequency": "monthly", "rate_plan": {"public_name": "Workers Paid"}}
        by, _ = self.run_cf(at(2026, 12, 3), gql(), subs=[sub])
        self.assertEqual(by["plan.cloudflare_workers"].status, Status.CRIT)

    def test_graphql_errors_raise(self):
        with self.assertRaises(RuntimeError):
            self.run_cf(NOW, {"errors": [{"message": "denied"}], "data": None})


class Others(unittest.TestCase):
    def test_github(self):
        http = FakeHttp({"https://api.github.com/organizations/o/settings/billing/usage": {"usageItems": [
            {"product": "Actions", "unitType": "Minutes", "quantity": 1700, "netAmount": 1.5},
            {"product": "Actions", "unitType": "Minutes", "quantity": 100, "netAmount": 0},
            {"product": "Packages", "unitType": "GigabyteHours", "quantity": 24.0, "netAmount": 0},
            {"product": "Copilot", "unitType": "Seats", "quantity": 1, "netAmount": 19}]}})
        r = billing_github.SOURCES[0].fetch(make_ctx(CFG, http=http, env={"OPS_GITHUB_TOKEN": "t"}, now=NOW))
        by = {i.key: i for i in r.items}
        self.assertEqual(by["billed.github"].values["amount"], 20.5)
        self.assertEqual(by["quota.gh_actions"].values["used"], 1800)
        self.assertEqual(by["quota.gh_actions"].status, Status.WARN)
        self.assertEqual(by["quota.gh_packages"].values["used"], round(24 / 12, 3))

    def test_hcp(self):
        base = "https://app.terraform.io/api/v2/organizations/o"
        http = FakeHttp({
            base + "/explorer": {"data": [{"attributes": {"current-rum-count": 10}},
                                          {"attributes": {"current-rum-count": None}},
                                          {"attributes": {"current-rum-count": 5}}],
                                 "meta": {"pagination": {"next-page": None}}},
            base: {"data": {"attributes": {"plan-identifier": "free_standard"}}}})
        r = billing_hcp_terraform.SOURCES[0].fetch(make_ctx(CFG, http=http, env={"OPS_TFC_TOKEN": "t"}, now=NOW))
        by = {i.key: i for i in r.items}
        self.assertEqual(by["quota.tfc_rum"].values["used"], 15)
        self.assertEqual(r.status, Status.OK)
        # organization トークンでは /subscription が 404 になるため、organization の属性で判定する
        self.assertNotIn(base + "/subscription", [c["url"] for c in http.calls])

    def test_hcp_paid_after_revert_is_crit(self):
        cfg = config.parse(TOML + """
[[plans]]
service = "hcp_terraform"
name = "Paid"
paid = true
from = "2019-01-01"
until = "2019-12-31"
""".replace("2019-01-01", "2019-01-01"))
        # 上記は重複しない過去の有料宣言 (Free は 2020 開始) と実プラン有料の組
        base = "https://app.terraform.io/api/v2/organizations/o"
        http = FakeHttp({base + "/explorer": {"data": [], "meta": {}},
                         base: {"data": {"attributes": {"plan-identifier": "plus"}}}})
        r = billing_hcp_terraform.SOURCES[0].fetch(make_ctx(cfg, http=http, env={"OPS_TFC_TOKEN": "t"}, now=NOW))
        self.assertEqual({i.key: i.status for i in r.items}["plan.hcp_terraform"], Status.CRIT)

    def test_tailscale(self):
        http = FakeHttp({
            "https://api.tailscale.com/api/v2/oauth/token": {"access_token": "AT"},
            "https://api.tailscale.com/api/v2/tailnet/-/users": {"users": [{}, {}, {}]},
            "https://api.tailscale.com/api/v2/tailnet/-/devices": {"devices": [{}] * 4}})
        env = {"OPS_TAILSCALE_OAUTH_CLIENT_ID": "i", "OPS_TAILSCALE_OAUTH_CLIENT_SECRET": "s"}
        r = billing_tailscale.SOURCES[0].fetch(make_ctx(CFG, http=http, env=env, now=NOW))
        by = {i.key: i for i in r.items}
        self.assertEqual(by["quota.tailscale_users"].status, Status.CRIT)
        self.assertEqual(by["quota.tailscale_devices"].values["used"], 4)
        self.assertEqual(http.calls[0]["data"]["client_id"], "i")
        self.assertEqual(http.calls[1]["bearer"], "AT")

    def test_netdata(self):
        b = "https://app.netdata.cloud/api/v2/spaces/sp/rooms"
        http = FakeHttp({b: [{"id": "r0", "name": "x"}, {"id": "r1", "name": "All nodes"}],
                         b + "/r1/nodes": {"nodes": [{}, {}]}})
        r = billing_netdata.SOURCES[0].fetch(
            make_ctx(CFG, http=http, env={"OPS_NETDATA_API_TOKEN": "t", "TF_VAR_netdata_space_id": "sp"}, now=NOW))
        self.assertEqual({i.key: i for i in r.items}["quota.netdata_nodes"].values["used"], 2)

    def test_infisical_declared_only_and_credentials(self):
        src = billing_infisical.SOURCES[0]
        self.assertIsNone(src.interval)
        r = src.render(make_ctx(CFG, now=NOW), {})
        by = {i.key: i for i in r.items}
        q = by["quota.infisical_identities"]
        self.assertEqual((q.values["used"], q.values["limit"]), (None, 5))
        self.assertIn("取得できない", q.note)
        self.assertEqual(by["cred.B"].status, Status.CRIT)
        self.assertEqual(r.status, Status.CRIT)


class HetznerOS(unittest.TestCase):
    def test_sigv4_aws_example(self):
        h = billing_hetzner_os.sigv4_headers(
            "examplebucket.s3.amazonaws.com", "/", {"lifecycle": ""}, "AKIAIOSFODNN7EXAMPLE",
            "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1", datetime(2013, 5, 24, tzinfo=timezone.utc))
        self.assertTrue(h["Authorization"].endswith(
            "Signature=fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"))

    def xml(self, sizes, truncated=False, token=None):
        c = "".join(f"<Contents><Key>k</Key><Size>{n}</Size></Contents>" for n in sizes)
        t = f"<NextContinuationToken>{token}</NextContinuationToken>" if token else ""
        return (f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{c}{t}</ListBucketResult>").encode()

    def test_totals_pagination_and_limit(self):
        pages = {}

        def b1(**kw):
            if kw["params"].get("continuation-token") == "T":
                return self.xml([500_000_000_000])
            return self.xml([300_000_000_000], True, "T")
        http = FakeHttp({"https://fsn1.your-objectstorage.com/b1": b1,
                         "https://fsn1.your-objectstorage.com/b2": self.xml([100_000_000_000])})
        env = {"HETZNER_OS_ACCESS_KEY_ID": "a", "HETZNER_OS_SECRET_ACCESS_KEY": "s"}
        r = billing_hetzner_os.SOURCES[0].fetch(make_ctx(CFG, http=http, env=env, now=NOW))
        by = {i.key: i for i in r.items}
        self.assertEqual(by["os.bucket.b1"].values["bytes"], 800_000_000_000)
        self.assertEqual(by["os.total"].values["used"], 900.0)
        self.assertEqual(by["os.total"].status, Status.WARN)
        self.assertIn("Credential=a/20260601/fsn1/s3/aws4_request", http.calls[0]["headers"]["Authorization"])


if __name__ == "__main__":
    unittest.main()
