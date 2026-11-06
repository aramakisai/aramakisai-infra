from datetime import timedelta

from model import Item, Source, Status, make_result

SOURCE_ID = "festival.quake"


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or SOURCE_ID in fc.disabled_sources:
        return make_result(SOURCE_ID, ctx.now(), [])
    r = ctx.http.get_json(f"{fc.quake_url}/status", bearer=ctx.secret("SIGNAGE_QUAKE_STATUS_TOKEN"))
    try:
        items = []
        # 改修前のリレーは cron を返さない。Item を作らず、評価器が未対応の応答として扱う
        if "cron" in r:
            items.append(Item("cron", "cron", Status.OK,
                              {"last_run_at": r["cron"]["lastRunAt"], "in_period": r["inPeriod"]}))
        for name, c in r["channels"].items():
            items.append(Item(f"channel.{name}", f"channel.{name}", Status.OK, {
                "connected": c["connected"], "receiving": c["receiving"],
                "last_received_at": c["lastReceivedAt"], "test": c["test"]}))
        d = r["discord"]
        items.append(Item("unreachable", "unreachable", Status.OK, {"value": r["unreachable"]}))
        items.append(Item("discord", "discord", Status.OK, {
            "configured": d["configured"], "disabled": d["disabled"], "last_success_at": d["lastSuccessAt"],
            "last_failure_at": d["lastFailureAt"], "last_failure": d["lastFailure"]}))
    except (KeyError, TypeError, AttributeError):
        raise ValueError("unexpected /status response") from None
    return make_result(SOURCE_ID, ctx.now(), items)


SOURCES = (Source(SOURCE_ID, timedelta(seconds=60), fetch),)
