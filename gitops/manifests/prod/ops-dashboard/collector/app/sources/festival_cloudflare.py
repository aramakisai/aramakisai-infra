from datetime import timedelta

from model import Item, Source, Status, iso, make_result
from sources.billing_cloudflare import ACCOUNT, GRAPHQL, _raise_on_errors

AE_SQL = f"{ACCOUNT}/{{account}}/analytics_engine/sql"

# 件数系の指標は、確定が遅い HTTP でも 9 割以上が揃う m+2:40 以降に読む。窓は [floor(now)-8分, floor(now)-3分)
WINDOW_END_LAG = timedelta(minutes=3)
WINDOW = timedelta(minutes=5)
# 接続数は出現した時点で確定するので、最新の分がこれより古ければ取得遅延
HUB_MAX_LAG = timedelta(minutes=4)
HUB_LOOKBACK = timedelta(minutes=10)

# 分位数は dimensions を付けるとその群ごとの値になる。全体の値は dimensions なしの alias (workersCpu・cmsLatency) で取る。
# clientIP・パス全体・User-Agent の次元は選ばない (個人を識別できる値を持たない)
QUERY = """
query($tag: string!, $start: Time, $end: Time, $hubStart: Time, $hubEnd: Time,
      $script: string, $cmsHost: string, $mediaLike: string, $hub: string) {
  viewer { accounts(filter: {accountTag: $tag}) {
    workers: workersInvocationsAdaptive(limit: 100, filter: {scriptName: $script, datetime_geq: $start, datetime_lt: $end}) {
      sum { requests errors } dimensions { status } }
    workersCpu: workersInvocationsAdaptive(limit: 1, filter: {scriptName: $script, datetime_geq: $start, datetime_lt: $end}) {
      quantiles { cpuTimeP99 } }
    cms: httpRequestsAdaptiveGroups(limit: 1000, filter: {clientRequestHTTPHost: $cmsHost, requestSource_in: ["eyeball", "edgeWorkerFetch"], datetime_geq: $start, datetime_lt: $end}) {
      count dimensions { edgeResponseStatus } }
    cmsLatency: httpRequestsAdaptiveGroups(limit: 1, filter: {clientRequestHTTPHost: $cmsHost, requestSource_in: ["eyeball", "edgeWorkerFetch"], datetime_geq: $start, datetime_lt: $end}) {
      quantiles { originResponseDurationMsP95 } }
    media: httpRequestsAdaptiveGroups(limit: 1000, filter: {clientRequestHTTPHost: $cmsHost, requestSource_in: ["eyeball", "edgeWorkerFetch"], clientRequestPath_like: $mediaLike, datetime_geq: $start, datetime_lt: $end}) {
      count dimensions { edgeResponseStatus } }
    cmsCache: httpRequestsAdaptiveGroups(limit: 100, filter: {clientRequestHTTPHost: $cmsHost, requestSource: "edgeWorkerCacheAPI", clientRequestHTTPMethodName: "GET", datetime_geq: $start, datetime_lt: $end}) {
      count dimensions { cacheStatus } }
    hub: durableObjectsPeriodicGroups(limit: 100, filter: {name: $hub, datetimeMinute_geq: $hubStart, datetimeMinute_lt: $hubEnd}) {
      max { activeWebsocketConnections } dimensions { datetimeMinute } }
  } }
}"""


def floor_minute(now):
    return now.replace(second=0, microsecond=0)


def window(now):
    """件数系の情報源が共有する時間窓 [start, end)。"""
    end = floor_minute(now) - WINDOW_END_LAG
    return end - WINDOW, end


def _count(rows, pred=lambda s: True):
    return sum(r["count"] for r in rows if pred(r["dimensions"]["edgeResponseStatus"]))


def _first(rows, group, key):
    return rows[0][group][key] if rows else 0


def fetch(ctx):
    now = ctx.now()
    fest = ctx.config.festival
    if fest is None:
        return make_result("festival.cloudflare", now, (), Status.EMPTY)
    token = ctx.secret("OPS_CLOUDFLARE_READ_TOKEN")
    account = ctx.secret("TF_VAR_cloudflare_account_id")
    start, end = window(now)
    cf = fest.cloudflare
    gql = ctx.http.post_json(GRAPHQL, {"query": QUERY, "variables": {
        "tag": account, "start": iso(start), "end": iso(end),
        "hubStart": iso(floor_minute(now) - HUB_LOOKBACK), "hubEnd": iso(floor_minute(now) + timedelta(minutes=1)),
        "script": cf.worker_script, "cmsHost": cf.cms_host, "mediaLike": cf.media_path_like, "hub": cf.hub_object_name}},
        bearer=token)
    _raise_on_errors(gql)
    a = gql["data"]["viewer"]["accounts"][0]
    we = iso(end)

    workers = a["workers"]
    cms, media, cache = a["cms"], a["media"], a["cmsCache"]
    is5 = lambda s: 500 <= s < 600
    items = [
        Item("worker", "Worker", Status.OK, {
            "requests": sum(r["sum"]["requests"] for r in workers),
            "errors": sum(r["sum"]["errors"] for r in workers),
            "exceeded": sum(r["sum"]["requests"] for r in workers if r["dimensions"]["status"] == "exceededResources"),
            "cpu_p99_ms": _first(a["workersCpu"], "quantiles", "cpuTimeP99") / 1000,
            "window_end": we}),
        Item("cms", "CMS", Status.OK, {
            "requests": _count(cms), "status_5xx": _count(cms, is5),
            "origin_p95_ms": _first(a["cmsLatency"], "quantiles", "originResponseDurationMsP95"), "window_end": we}),
        Item("media", "メディア", Status.OK, {
            "requests": _count(media), "status_4xx": _count(media, lambda s: 400 <= s < 500),
            "status_5xx": _count(media, is5), "window_end": we}),
        Item("cms_cache", "CMS キャッシュ", Status.OK, {
            "lookups": sum(r["count"] for r in cache),
            "hits": sum(r["count"] for r in cache if r["dimensions"]["cacheStatus"] == "hit"),
            "window_end": we}),
    ]

    by_minute = {}
    for r in a["hub"]:
        m = r["dimensions"]["datetimeMinute"]
        by_minute[m] = by_minute.get(m, 0) + r["max"]["activeWebsocketConnections"]
    latest = max(by_minute, default=None)
    stale = latest is None or latest < iso(floor_minute(now) - HUB_MAX_LAG)
    items.append(Item("hub_total", "サイネージハブ", Status.OK,
                      {"connections": by_minute.get(latest, 0), "minute": latest},
                      "取得遅延" if stale else None))
    return make_result("festival.cloudflare", now, items)


SOURCES = (Source("festival.cloudflare", timedelta(seconds=60), fetch),)
