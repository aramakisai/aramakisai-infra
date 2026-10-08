import json
import re
from datetime import timedelta

from model import Item, Source, Status, make_result, parse_iso
from render.labels import label
from sources.node_resources import COLLECTOR_NODE

CHANNELS_URL = "https://update.k3s.io/v1-release/channels"
STATE_FILE = "/var/lib/ops-dashboard/node-status.json"
STALE_AFTER = timedelta(hours=48)


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _node_status(ctx, path):
    try:
        with open(path) as f:
            d = json.load(f)
        age = ctx.now() - parse_iso(d["generated_at"])
    except FileNotFoundError:
        return Item("os", COLLECTOR_NODE, Status.STALE, {}, "ノード状態ファイルがありません")
    except (ValueError, KeyError, TypeError, AttributeError):
        # 状態ファイルの不備で k3s の判定まで ERROR にしない
        return Item("os", COLLECTOR_NODE, Status.STALE, {}, "ノード状態ファイルを読めません")
    keys = ("generated_at", "upgradable_count", "security_fixable_count", "reboot_required")
    values = {k: d.get(k) for k in keys}
    if age > STALE_AFTER:
        return Item("os", COLLECTOR_NODE, Status.STALE, values, "情報が古くなっています")
    # security_fixable_count は debsecan が使えないとき null (不明) になる
    pending = (d.get("security_fixable_count") or 0) > 0 or bool(d.get("reboot_required"))
    return Item("os", COLLECTOR_NODE, Status.WARN if pending else Status.OK, values)


def fetch(ctx):
    versions = sorted({n["status"]["nodeInfo"]["kubeletVersion"] for n in ctx.k8s.get("/api/v1/nodes")["items"]},
                      key=_ver)
    running = " / ".join(versions)
    channels = ctx.http.get_json(CHANNELS_URL)["data"]
    latest = next(c["latest"] for c in channels if c["id"] == "stable")
    outdated = _ver(versions[0]) < _ver(latest)
    mixed = len(versions) > 1
    path = ctx.config.source_settings("node.maintenance").get("state_file", STATE_FILE)
    return make_result("node.maintenance", ctx.now(), [
        Item("k3s", "k3s", Status.WARN if outdated or mixed else Status.OK,
             {"running": running, "latest": latest},
             label("note.k3s_update") if outdated else label("note.k3s_mixed") if mixed else None),
        _node_status(ctx, path),
    ])


SOURCES = (Source("node.maintenance", timedelta(hours=1), fetch),)
