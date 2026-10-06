from datetime import timedelta

import falco_ingest
import model
from model import Item, Source, Status
from sources import _buckets

PRIORITIES = ("emergency", "alert", "critical", "error", "warning", "notice", "informational", "debug")
RECENT_LIMIT = 50


def render(ctx, query):
    now = ctx.now()
    unit, n = _buckets.RANGES[_buckets.pick(query, "falco")]
    start = model.iso(_buckets.window_start_utc(now, unit, n))
    bucket = _buckets.sql_expr(unit, "received_at")

    counts = {k: dict.fromkeys(PRIORITIES, 0) for k in _buckets.keys(now, unit, n)}
    for r in ctx.store.query(
            f"SELECT {bucket} AS b, lower(priority) AS p, count(*) AS c FROM falco_events "
            "WHERE received_at >= ? GROUP BY b, p", (start,)):
        if r["b"] in counts and r["p"] in PRIORITIES:
            counts[r["b"]][r["p"]] += r["c"]
    items = [Item(f"falco.series.{k}", k, Status.OK, v) for k, v in counts.items()]

    for r in ctx.store.query(
            "SELECT rule, count(*) AS c FROM falco_events WHERE received_at >= ? "
            "GROUP BY rule ORDER BY c DESC, rule LIMIT 10", (start,)):
        items.append(Item(f"falco.rule.{r['rule']}", r["rule"], Status.OK, {"count": r["c"]}))

    for r in ctx.store.query(
            "SELECT id, time, received_at, priority, rule, k8s_ns, k8s_pod, container, output FROM falco_events "
            "WHERE received_at >= ? ORDER BY id DESC LIMIT ?", (start, RECENT_LIMIT)):
        target = "/".join(p for p in (r["k8s_ns"], r["k8s_pod"], r["container"]) if p)
        items.append(Item(f"falco.recent.{r['id']}", r["rule"], Status.OK, {
            "time": r["time"] or r["received_at"], "rule": r["rule"], "priority": (r["priority"] or "").lower(),
            "target": target, "summary": (r["output"] or "")[:300]}))

    failed = falco_ingest.store_failures()
    items.append(Item("falco.ingest_failed", "ingest_failed", Status.WARN if failed else Status.OK, {"count": failed}))
    return model.make_result("security.falco", now, items)


SOURCES = (Source("security.falco", None, None, render=render),)
