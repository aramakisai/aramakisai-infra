from datetime import timedelta

from model import Source, make_result
from plan import jst_today, plan_item, quota_item

API = "https://app.netdata.cloud/api/v2"


def _nodes(d):
    return d if isinstance(d, list) else d.get("nodes", [])


def fetch(ctx):
    token = ctx.secret("OPS_NETDATA_API_TOKEN")
    space = ctx.secret("TF_VAR_netdata_space_id")
    now = ctx.now()
    rooms = ctx.http.get_json(f"{API}/spaces/{space}/rooms", bearer=token)
    room = next(r for r in rooms if r["name"] == "All nodes")
    count = len(_nodes(ctx.http.get_json(f"{API}/spaces/{space}/rooms/{room['id']}/nodes", bearer=token)))

    plan = ctx.config.active_plan("netdata_cloud", jst_today(now))
    items = [plan_item(ctx.config, "netdata_cloud", now)]
    if plan:
        items.append(quota_item("quota.netdata_nodes", "quota.netdata_nodes", count, plan.limits.get("nodes"),
                                "nodes", ctx.config.thresholds.warn_ratio))
    return make_result("billing.netdata", now, items)


SOURCES = (Source("billing.netdata", timedelta(hours=6), fetch),)
