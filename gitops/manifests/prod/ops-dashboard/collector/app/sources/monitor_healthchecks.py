from datetime import timedelta

from model import Item, Source, Status, make_result
from sources._common import quota_item

URL = "https://healthchecks.io/api/v3/checks/"
_STATUS = {"up": Status.OK, "grace": Status.WARN, "down": Status.CRIT, "new": Status.STALE, "paused": Status.WARN}


def fetch(ctx):
    r = ctx.http.get_json(URL, headers={"X-Api-Key": ctx.secret("OPS_HEALTHCHECKS_READONLY_KEY")})
    checks = r["checks"]
    items = [quota_item(ctx, "healthchecks", "checks", len(checks))]
    for c in checks:
        state = c["status"]
        items.append(Item(f"check.{c.get('uuid') or c['name']}", c["name"], _STATUS.get(state, Status.WARN),
                          {"state": state, "last_ping": c.get("last_ping")}))
    return make_result("monitor.healthchecks", ctx.now(), items)


SOURCES = (Source("monitor.healthchecks", timedelta(minutes=5), fetch),)
