from datetime import timedelta

from model import Source, make_result
from plan import jst_today, plan_item, quota_item

API = "https://api.tailscale.com/api/v2"


def fetch(ctx):
    cid, secret = ctx.secret("OPS_TAILSCALE_OAUTH_CLIENT_ID"), ctx.secret("OPS_TAILSCALE_OAUTH_CLIENT_SECRET")
    now = ctx.now()
    # "-" は OAuth クライアントが属する tailnet を指す。
    tailnet = ctx.config.source_settings("billing.tailscale").get("tailnet", "-")
    token = ctx.http.post_form(f"{API}/oauth/token", {
        "grant_type": "client_credentials", "client_id": cid, "client_secret": secret})["access_token"]
    users = len(ctx.http.get_json(f"{API}/tailnet/{tailnet}/users", bearer=token)["users"])
    devices = len(ctx.http.get_json(f"{API}/tailnet/{tailnet}/devices", bearer=token)["devices"])

    plan = ctx.config.active_plan("tailscale", jst_today(now))
    items = [plan_item(ctx.config, "tailscale", now)]
    if plan:
        warn = ctx.config.thresholds.warn_ratio
        items += [quota_item("quota.tailscale_users", "quota.tailscale_users", users, plan.limits.get("users"),
                             "users", warn),
                  quota_item("quota.tailscale_devices", "quota.tailscale_devices", devices,
                             plan.limits.get("devices"), "devices", warn)]
    return make_result("billing.tailscale", now, items)


SOURCES = (Source("billing.tailscale", timedelta(hours=1), fetch),)
