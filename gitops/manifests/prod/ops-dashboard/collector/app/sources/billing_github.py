from datetime import timedelta, timezone

from model import Item, Source, Status, make_result
from plan import jst_today, plan_item, quota_item

API = "https://api.github.com"


def fetch(ctx):
    token = ctx.secret("OPS_GITHUB_TOKEN")
    org = ctx.config.source_settings("billing.github")["org"]
    now = ctx.now()
    utc = now.astimezone(timezone.utc)
    d = ctx.http.get_json(
        f"{API}/organizations/{org}/settings/billing/usage",
        params={"year": str(utc.year), "month": str(utc.month)}, bearer=token,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    rows = d.get("usageItems", [])
    net = sum(r.get("netAmount") or 0 for r in rows)
    minutes = sum(r.get("quantity") or 0 for r in rows
                  if r.get("product", "").lower() == "actions" and r.get("unitType", "").lower() == "minutes")
    # Packages の保存量は GB·時間で報告されるため、当月の経過時間で割って平均 GB にする。
    gb_hours = sum(r.get("quantity") or 0 for r in rows
                   if r.get("product", "").lower() == "packages" and "hour" in r.get("unitType", "").lower())
    elapsed = max(1.0, (utc - utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600)

    plan = ctx.config.active_plan("github", jst_today(now))
    warn = ctx.config.thresholds.warn_ratio
    items = [Item("billed.github", "GitHub", Status.OK, {"amount": round(net, 2), "currency": "USD"}),
             plan_item(ctx.config, "github", now)]
    if plan:
        items += [quota_item("quota.gh_actions", "quota.gh_actions", minutes,
                             plan.limits.get("actions_minutes_month"), "min", warn),
                  quota_item("quota.gh_packages", "quota.gh_packages", round(gb_hours / elapsed, 3),
                             plan.limits.get("packages_storage_gb"), "GB", warn)]
    return make_result("billing.github", now, items)


SOURCES = (Source("billing.github", timedelta(hours=6), fetch),)
