from datetime import timedelta

from model import Item, Source, Status, make_result


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None:
        return make_result("festival.schedule", ctx.now(), [], Status.EMPTY)
    s = ctx.http.get_json(f"{fc.cms.service_base}/api/globals/site_settings", params={"depth": "0"})
    pm = s["parking_monitor"]
    items = [Item("monitor", "parking_monitor", Status.OK, {"start": pm["start"], "end": pm["end"]})]
    for n, d in enumerate(s["event_days"]):
        items.append(Item(f"event_day.{n}", d.get("label") or f"event_day.{n}", Status.OK,
                          {"start_at": d["start_at"], "end_at": d["end_at"]}))
    return make_result("festival.schedule", ctx.now(), items)


SOURCES = (Source("festival.schedule", timedelta(minutes=5), fetch),)
