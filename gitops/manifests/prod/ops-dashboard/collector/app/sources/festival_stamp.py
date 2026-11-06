from datetime import timedelta

from model import Item, Source, Status, iso, make_result
from sources.festival_cloudflare import AE_SQL, window

# プレビューも本番の Worker 名で動くため blob1 = 'prod' が必須。blob3 が outcome、double7 が d1Failed
AE_QUERY = (
    "SELECT SUM(IF(blob3 = 'failed', _sample_interval, 0)) AS failed, "
    "SUM(IF(blob3 = 'rate_limited', _sample_interval, 0)) AS rate_limited, "
    "SUM(IF(double7 > 0, _sample_interval, 0)) AS d1_failed "
    "FROM aramakisai_stamp_metrics "
    "WHERE blob1 = 'prod' AND timestamp >= toDateTime('{start}') AND timestamp < toDateTime('{end}')")


def fetch(ctx):
    now = ctx.now()
    if ctx.config.festival is None:
        return make_result("festival.stamp", now, (), Status.EMPTY)
    token = ctx.secret("OPS_CLOUDFLARE_READ_TOKEN")
    account = ctx.secret("TF_VAR_cloudflare_account_id")
    start, end = window(now)
    fmt = "%Y-%m-%d %H:%M:%S"
    resp = ctx.http.post_text(AE_SQL.format(account=account),
                              AE_QUERY.format(start=start.strftime(fmt), end=end.strftime(fmt)), bearer=token)
    rows = resp["data"]
    row = rows[0] if rows else {}
    n = lambda k: int(float(row.get(k) or 0))
    return make_result("festival.stamp", now, [Item("stamp", "スタンプ", Status.OK, {
        "failed": n("failed"), "rate_limited": n("rate_limited"), "d1_failed": n("d1_failed"),
        "window_end": iso(end)})])


SOURCES = (Source("festival.stamp", timedelta(seconds=60), fetch),)
