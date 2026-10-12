from datetime import datetime, timedelta, timezone

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
query($tag: string!, $monthStart: Time, $now: Time) {
  viewer { accounts(filter: {accountTag: $tag}) {
    storage: r2StorageAdaptiveGroups(limit: 10000, filter: {datetime_geq: $monthStart, datetime_leq: $now}, orderBy: [datetime_DESC]) { max { payloadSize metadataSize } dimensions { datetime bucketName } }
    ops: r2OperationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $monthStart, datetime_leq: $now}) { sum { requests } dimensions { actionType } }
  } }
}"""

# 範囲 [start, end) の Workers と Durable Objects の使用量。durableObjectsPeriodicGroups は datetime ではなく date で絞る
USAGE_QUERY = """
query($tag: string!, $start: Time, $end: Time, $startDate: Date, $endDate: Date) {
  viewer { accounts(filter: {accountTag: $tag}) {
    workers: workersInvocationsAdaptive(limit: 10000, filter: {datetime_geq: $start, datetime_lt: $end}) { sum { requests cpuTimeUs } }
    doInv: durableObjectsInvocationsAdaptiveGroups(limit: 10000, filter: {datetime_geq: $start, datetime_lt: $end}) { sum { requests } }
    doPeriodic: durableObjectsPeriodicGroups(limit: 10000, filter: {date_geq: $startDate, date_leq: $endDate}) { sum { duration inboundWebsocketMsgCount } }
  } }
}"""

# GraphQL Analytics は 1 回の問い合わせの時間範囲が 4w4d (32 日) までなので、余裕を見て 30 日で区切る
_WINDOW = timedelta(days=30)

# (プランの limits のキー, 項目キー, 集計の範囲, 単位, 使用量の取り出し)
_USAGE_QUOTAS = (
    ("requests_day", "quota.workers_day", "day", "req", lambda u: u["requests"]),
    ("requests_month", "quota.workers_month", "month", "req", lambda u: u["requests"]),
    ("cpu_ms_month", "quota.workers_cpu_month", "month", "ms", lambda u: u["cpu_us"] / 1000),
    ("do_requests_day", "quota.do_requests_day", "day", "req", lambda u: u["do_requests"]),
    ("do_requests_month", "quota.do_requests_month", "month", "req", lambda u: u["do_requests"]),
    ("do_gbs_day", "quota.do_gbs_day", "day", "GB-s", lambda u: u["gbs"]),
    ("do_gbs_month", "quota.do_gbs_month", "month", "GB-s", lambda u: u["gbs"]),
)


def _usage(ctx, token, account, start, end):
    """[start, end) の使用量。範囲が広いときは窓に分けて足す。"""
    total = {"requests": 0, "cpu_us": 0, "do_requests": 0, "gbs": 0.0}
    ws = 0
    cur = start
    while cur < end:
        nxt = min(cur + _WINDOW, end)
        gql = ctx.http.post_json(GRAPHQL, {"query": USAGE_QUERY, "variables": {
            "tag": account, "start": iso(cur), "end": iso(nxt),
            "startDate": cur.date().isoformat(), "endDate": (nxt - timedelta(seconds=1)).date().isoformat()}},
            bearer=token)
        _raise_on_errors(gql)
        acct = gql["data"]["viewer"]["accounts"][0]
        total["requests"] += sum(r["sum"]["requests"] for r in acct["workers"])
        total["cpu_us"] += sum(r["sum"]["cpuTimeUs"] for r in acct["workers"])
        total["do_requests"] += sum(r["sum"]["requests"] for r in acct["doInv"])
        total["gbs"] += sum(r["sum"]["duration"] for r in acct["doPeriodic"])
        ws += sum(r["sum"]["inboundWebsocketMsgCount"] for r in acct["doPeriodic"])
        cur = nxt
    # 請求では受信の WebSocket メッセージ 20 件が 1 要求。requests は hibernation 中の受信を 1 件ずつ数えるため 19/20 を引く
    total["do_requests"] = max(0, round(total["do_requests"] - ws * 19 / 20))
    return total


def _raise_on_errors(gql):
    if gql.get("errors"):
        raise RuntimeError("GraphQL errors: " + "; ".join(str(e.get("message")) for e in gql["errors"])[:200])


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
        "tag": account, "now": iso(now),
        "monthStart": iso(utc.replace(day=1, hour=0, minute=0, second=0))}}, bearer=token)
    _raise_on_errors(gql)
    acct = gql["data"]["viewer"]["accounts"][0]
    zt = ctx.http.get_json(f"{ACCOUNT}/{account}/access/users", params={"per_page": "1"}, bearer=token)
    zt_users = (zt.get("result_info") or {}).get("total_count", len(zt.get("result", [])))

    items = [Item("billed.cloudflare", "Cloudflare", Status.OK, {"amount": round(monthly, 2),
                                                                         "currency": "USD"}),
             plan_item(cfg, "cloudflare_workers", now, workers_paid),
             plan_item(cfg, "cloudflare_r2", now), plan_item(cfg, "cloudflare_zero_trust", now)]

    today = jst_today(now)
    wp = cfg.active_plan("cloudflare_workers", today)
    if wp:
        # 日次は Cloudflare の上限がリセットされる UTC の 0 時から。月次は請求の月が契約日から数えられるため、
        # 暦の月初ではなくプランの from の 0 時 (UTC) から
        starts = {"day": utc.replace(hour=0, minute=0, second=0, microsecond=0),
                  "month": datetime(wp.from_.year, wp.from_.month, wp.from_.day, tzinfo=timezone.utc)}
        usages = {}
        for limit_key, key, scope, unit, pick in _USAGE_QUOTAS:
            if limit_key not in wp.limits:
                continue
            if scope not in usages:
                usages[scope] = _usage(ctx, token, account, starts[scope], utc)
            items.append(quota_item(key, key, pick(usages[scope]), wp.limits[limit_key], unit, warn))

    r2 = cfg.active_plan("cloudflare_r2", today)
    if r2:
        latest = {}  # 新しい順に返るので、バケットごとに最初の行が最新
        for r in acct["storage"]:
            latest.setdefault(r["dimensions"]["bucketName"], r["max"])
        gb = sum(m["payloadSize"] + m["metadataSize"] for m in latest.values()) / 1e9
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
