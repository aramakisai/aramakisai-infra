import unittest
from datetime import date

from helpers import FIXED_NOW, make_ctx
from model import Item, SourceResult, Status, make_result
from plan import credential_items, plan_item, quota_item
from render.sections import billing

CFG = make_ctx().config


def res(sid, items):
    return make_result(sid, FIXED_NOW, items)


def hetzner(missing=False):
    items = [Item("hetzner.server.prod-node-1", "prod-node-1", Status.OK,
                  {"type": "cx43", "location": "fsn1", "created": "2026-05-20T01:00:00Z", "state": "running",
                   "outgoing_bytes": 5_000_000_000, "included_bytes": 20_000_000_000_000, "cost": 9.0}),
             Item("hetzner.server.tmp-1", "tmp-1", Status.CRIT,
                  {"type": "cx22", "location": "fsn1", "created": "2026-05-20T01:00:00Z", "state": "running"},
                  "宣言にないサーバーが稼働しています"),
             Item("hetzner.estimate", "Hetzner Cloud", Status.OK,
                  {"amount": 12.34, "currency": "EUR", "outgoing_bytes": 5_000_000_000,
                   "included_bytes": 20_000_000_000_000})]
    return res("billing.hetzner", items)


class Billing(unittest.TestCase):
    def snap(self):
        warn = CFG.thresholds.warn_ratio
        return {
            "billing.hetzner": hetzner(),
            "billing.cloudflare": res("billing.cloudflare", [
                Item("billed.cloudflare", "Cloudflare", Status.OK, {"amount": 5.0, "currency": "USD"}),
                plan_item(CFG, "cloudflare_workers", FIXED_NOW),
                quota_item("quota.workers_day", "quota.workers_day", 90_000, 100_000, "req", warn)]),
            "billing.github": res("billing.github", [
                Item("billed.github", "GitHub", Status.OK, {"amount": 1.5, "currency": "USD"}),
                plan_item(CFG, "github", FIXED_NOW),
                quota_item("quota.gh_packages", "quota.gh_packages", 0.5, 2, "GB", warn)]),
            "billing.hetzner_os": res("billing.hetzner_os", [
                quota_item("os.total", "sub.object_storage", 300.0, 1000, "GB", warn),
                Item("os.bucket.bk-a", "bk-a", Status.OK, {"bytes": 200_000_000_000}),
                Item("os.bucket.bk-b", "bk-b", Status.OK, {"bytes": 100_000_000_000})]),
            "billing.infisical": res("billing.infisical", [
                plan_item(CFG, "infisical", FIXED_NOW),
                quota_item("quota.infisical_identities", "quota.infisical_identities", None, 5, "identities", warn,
                           note="API で取得できないため使用量は表示しません (上限は宣言値)")]
                + credential_items(CFG, FIXED_NOW)),
            "monitor.uptimerobot": res("monitor.uptimerobot", [
                Item("quota", "uptimerobot", Status.WARN, {"used": 45, "limit": 50}, "警告閾値 (80%) を超えています"),
                Item("monitor.1", "web", Status.OK, {"state": "up"})]),
        }

    def test_hetzner(self):
        h = billing.render(self.snap(), {})
        for s in ("prod-node-1", "cx43", "fsn1", "€12.34", "宣言にないサーバーが稼働しています", "tmp-1"):
            self.assertIn(s, h)
        self.assertIn("5.0 GB", h)
        self.assertIn("20.00 TB", h)

    def test_billed_and_plan(self):
        h = billing.render(self.snap(), {})
        self.assertIn("契約中のプラン料金 (月額): $5.00", h)
        self.assertIn("当月の請求見込み: $1.50", h)
        self.assertIn("請求額を取得する API がありません", h)
        self.assertIn("Cloudflare Workers", h)

    def test_quota_ratio_and_status_text(self):
        h = billing.render(self.snap(), {})
        self.assertIn("90.0%", h)
        self.assertIn("警告", h)
        self.assertIn("UptimeRobot モニター数", h)
        self.assertIn("90.0%", h)

    def test_declared_only_has_no_usage(self):
        h = billing.render(self.snap(), {})
        self.assertIn("API で取得できないため使用量は表示しません", h)

    def test_object_storage(self):
        h = billing.render(self.snap(), {})
        self.assertIn("使用容量 (合計): 300.0 GB / 基本料金に含まれる容量 1.00 TB", h)
        self.assertIn("bk-a: 200.0 GB", h)
        self.assertIn("bk-b: 100.0 GB", h)

    def test_credentials_table(self):
        h = billing.render(self.snap(), {})
        self.assertIn("資格情報の期限", h)
        for c in CFG.credentials:
            self.assertIn(c.name, h)
            self.assertIn(c.expires_on.isoformat(), h)

    def test_plan_revert_is_crit_text(self):
        late = FIXED_NOW.replace(year=2026, month=12, day=15)
        s = {"billing.cloudflare": res("billing.cloudflare", [plan_item(CFG, "cloudflare_workers", late, True)])}
        # 宣言上は無料に戻っているが実プランが有料のまま
        h = billing.render(s, {})
        self.assertIn("プランの戻し忘れ", h)
        self.assertIn("異常", h)

    def test_error_and_collecting(self):
        err = SourceResult("billing.github", Status.ERROR, FIXED_NOW, None, (), "boom")
        h = billing.render({"billing.github": err}, {})
        self.assertIn("取得失敗", h)
        self.assertIn("boom", h)
        self.assertIn("収集中", h)

    def test_escape(self):
        r = res("billing.hetzner", [Item("hetzner.server.<b>", "<b>", Status.OK,
                                         {"type": "<i>", "location": "x", "created": "2026-05-20T01:00:00Z"})])
        h = billing.render({"billing.hetzner": r}, {})
        self.assertNotIn("<b>", h)
        self.assertNotIn("<i>", h)


if __name__ == "__main__":
    unittest.main()
