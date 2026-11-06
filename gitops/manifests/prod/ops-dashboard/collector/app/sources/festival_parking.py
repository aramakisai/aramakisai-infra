from datetime import timedelta

from model import Item, Source, Status, make_result

_FIELDS = ("name", "lot", "last_captured_at", "device_received_at", "judge_last_succeeded_at",
           "judge_last_failed_at", "judge_last_error", "judge_consecutive_failures", "lot_status_updated_at")


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or not fc.parking.enabled:
        return make_result("festival.parking", ctx.now(), [], Status.EMPTY)
    r = ctx.http.get_json(f"{fc.cms.service_base}/api/ops/parking-health",
                          bearer=ctx.secret("OPS_PARKING_HEALTH_TOKEN"))
    cameras = r["cameras"]
    return make_result("festival.parking", ctx.now(), [
        Item(f"camera.{c['id']}", c["name"], Status.OK, {k: c[k] for k in _FIELDS}) for c in cameras])


SOURCES = (Source("festival.parking", timedelta(seconds=60), fetch),)
