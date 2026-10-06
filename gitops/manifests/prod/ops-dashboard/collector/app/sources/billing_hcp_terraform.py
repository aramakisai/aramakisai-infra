from datetime import timedelta

from model import Item, Source, Status, make_result
from plan import jst_today, plan_item, quota_item

API = "https://app.terraform.io/api/v2"


def fetch(ctx):
    token = ctx.secret("OPS_TFC_TOKEN")
    org = ctx.config.source_settings("billing.hcp_terraform")["org"]
    now = ctx.now()
    hdr = {"Content-Type": "application/vnd.api+json"}

    rum, page = 0, 1
    while page:
        d = ctx.http.get_json(f"{API}/organizations/{org}/explorer",
                              params={"type": "workspaces", "page[size]": "100", "page[number]": str(page)},
                              bearer=token, headers=hdr)
        rum += sum(w["attributes"].get("current-rum-count") or 0 for w in d["data"])
        page = ((d.get("meta") or {}).get("pagination") or {}).get("next-page")

    sub = ctx.http.get_json(f"{API}/organizations/{org}/subscription", bearer=token, headers=hdr)
    fs = next((i["attributes"] for i in sub.get("included", []) if i.get("type") == "feature-sets"), None)
    actual_paid = None
    if fs:
        actual_paid = not (fs.get("name", "").lower().startswith("free")
                           or fs.get("identifier", "").lower().startswith("free"))

    plan = ctx.config.active_plan("hcp_terraform", jst_today(now))
    items = [plan_item(ctx.config, "hcp_terraform", now, actual_paid)]
    if plan:
        items.append(quota_item("quota.tfc_rum", "quota.tfc_rum", rum, plan.limits.get("managed_resources"),
                                "resources", ctx.config.thresholds.warn_ratio))
    return make_result("billing.hcp_terraform", now, items)


SOURCES = (Source("billing.hcp_terraform", timedelta(hours=6), fetch),)
