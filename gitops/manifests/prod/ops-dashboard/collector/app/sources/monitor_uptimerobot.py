from datetime import timedelta

from model import Item, Source, Status, make_result
from sources._common import quota_item

URL = "https://api.uptimerobot.com/v2/getMonitors"
PAGE = 50
# v2 の status コード。1 (未確認) と 8 (down の疑い) は確定するまで利用者には一時的な状態として扱う。
_STATE = {0: ("paused", Status.WARN), 1: ("pending", Status.STALE), 2: ("up", Status.OK),
          8: ("down", Status.CRIT), 9: ("down", Status.CRIT)}


def fetch(ctx):
    key = ctx.secret("OPS_UPTIMEROBOT_READONLY_KEY")
    monitors, offset = [], 0
    while True:
        r = ctx.http.post_form(URL, {"api_key": key, "format": "json", "limit": str(PAGE), "offset": str(offset)})
        if r.get("stat") != "ok":
            raise RuntimeError("UptimeRobot getMonitors failed")
        monitors += r["monitors"]
        offset += PAGE
        if offset >= r.get("pagination", {}).get("total", len(monitors)) or not r["monitors"]:
            break
    items = [quota_item(ctx, "uptimerobot", "monitors", len(monitors))]
    for m in monitors:
        state, status = _STATE.get(m["status"], ("unknown", Status.WARN))
        items.append(Item(f"monitor.{m['id']}", m["friendly_name"], status, {"state": state}))
    return make_result("monitor.uptimerobot", ctx.now(), items)


SOURCES = (Source("monitor.uptimerobot", timedelta(minutes=5), fetch),)
