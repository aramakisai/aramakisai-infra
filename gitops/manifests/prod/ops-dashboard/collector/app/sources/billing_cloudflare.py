from datetime import timedelta, timezone

from model import Item, Source, Status, iso, make_result
from plan import jst_today, plan_item, quota_item

ACCOUNT = "https://api.cloudflare.com/client/v4/accounts"
GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"

CLASS_A = {"ListBuckets", "PutBucket", "ListObjects", "PutObject", "CopyObject", "CompleteMultipartUpload",
           "CreateMultipartUpload", "LifecycleStorageTierTransition", "ListMultipartUploads", "UploadPart",
           "UploadPartCopy", "ListParts", "PutBucketEncryption", "PutBucketCors", "PutBucketLifecycleConfiguration"}
CLASS_B = {"HeadBucket", "HeadObject", "GetObject", "UsageSummary", "GetBucketEncryption", "GetBucketLocation",
           "GetBucketCors", "GetBucketLifecycleConfiguration"}

_PER_MONTH = {"weekly": 52 / 12, "monthly": 1, "quarterly": 1 / 3, "yearly": 1 / 12}

QUERY = """
query($tag: string!, $dayStart: Time, $monthStart: Time, $now: Time) {
  viewer { accounts(filter: {accountTag: $tag}) {
    day: workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $dayStart, datetime_leq: $now}) { sum { requests } }
    month: workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $monthStart, datetime_leq: $now}) { sum { requests } }
    storage: r2StorageAdaptiveGroups(limit: 1, filter: {datetime_geq: $monthStart, datetime_leq: $now}, orderBy: [datetime_DESC]) { max { payloadSize metadataSize } }
    ops: r2OperationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $monthStart, datetime_leq: $now}) { sum { requests } dimensions { actionType } }
  } }
}"""


def fetch(ctx):
    token = ctx.secret("OPS_CLOUDFLARE_READ_TOKEN")
    account = ctx.secret("TF_VAR_cloudflare_account_id")
    now = ctx.now()
    cfg, warn = ctx.config, ctx.config.thresholds.warn_ratio

    subs = ctx.http.get_json(f"{ACCOUNT}/{account}/subscriptions", bearer=token)["result"]
    paid = [s for s in subs if s.get("state") == "Paid" and (s.get("price") or 0) > 0]
    monthly = sum(s["price"] * _PER_MONTH.get(s.get("frequency"), 1) for s in paid)
    workers_paid = any("workers" in (s.get("rate_plan") or {}).get("public_name", "").lower() for s in paid)

    utc = now.astimezone(timezone.utc)
    gql = ctx.http.post_json(GRAPHQL, {"query": QUERY, "variables": {
        "tag": account, "now": iso(now), "dayStart": iso(utc.replace(hour=0, minute=0, second=0)),
        "monthStart": iso(utc.replace(day=1, hour=0, minute=0, second=0))}}, bearer=token)
    if gql.get("errors"):
        raise RuntimeError("GraphQL errors: " + "; ".join(str(e.get("message")) for e in gql["errors"])[:200])
    acct = gql["data"]["viewer"]["accounts"][0]
    zt = ctx.http.get_json(f"{ACCOUNT}/{account}/access/users", params={"per_page": "1"}, bearer=token)
    zt_users = (zt.get("result_info") or {}).get("total_count", len(zt.get("result", [])))

    items = [Item("billed.cloudflare", "Cloudflare", Status.OK, {"amount": round(monthly, 2),
                                                                         "currency": "USD"}),
             plan_item(cfg, "cloudflare_workers", now, workers_paid),
             plan_item(cfg, "cloudflare_r2", now), plan_item(cfg, "cloudflare_zero_trust", now)]

    today = jst_today(now)
    wp = cfg.active_plan("cloudflare_workers", today)
    if wp and "requests_month" in wp.limits:
        used = sum(r["sum"]["requests"] for r in acct["month"])
        items.append(quota_item("quota.workers_month", "quota.workers_month", used, wp.limits["requests_month"],
                                "req", warn))
    elif wp and "requests_day" in wp.limits:
        used = sum(r["sum"]["requests"] for r in acct["day"])
        items.append(quota_item("quota.workers_day", "quota.workers_day", used, wp.limits["requests_day"],
                                "req", warn))

    r2 = cfg.active_plan("cloudflare_r2", today)
    if r2:
        st = acct["storage"][0]["max"] if acct["storage"] else {"payloadSize": 0, "metadataSize": 0}
        gb = (st["payloadSize"] + st["metadataSize"]) / 1e9
        a = sum(r["sum"]["requests"] for r in acct["ops"] if r["dimensions"]["actionType"] in CLASS_A)
        b = sum(r["sum"]["requests"] for r in acct["ops"] if r["dimensions"]["actionType"] in CLASS_B)
        items += [quota_item("quota.r2_storage", "quota.r2_storage", gb, r2.limits.get("storage_gb"), "GB", warn),
                  quota_item("quota.r2_class_a", "quota.r2_class_a", a, r2.limits.get("class_a_month"), "req", warn),
                  quota_item("quota.r2_class_b", "quota.r2_class_b", b, r2.limits.get("class_b_month"), "req", warn)]
    zp = cfg.active_plan("cloudflare_zero_trust", today)
    if zp:
        items.append(quota_item("quota.zt_users", "quota.zt_users", zt_users, zp.limits.get("users"), "users", warn))
    return make_result("billing.cloudflare", now, items)


SOURCES = (Source("billing.cloudflare", timedelta(hours=1), fetch),)
