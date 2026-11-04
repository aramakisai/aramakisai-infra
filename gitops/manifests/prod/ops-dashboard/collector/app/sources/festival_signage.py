from datetime import timedelta

from model import Item, Source, Status, make_result

SOURCE_ID = "festival.signage"
_ORIGINS = ("prod", "dev", "preview", "unknown")


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or SOURCE_ID in fc.disabled_sources:
        return make_result(SOURCE_ID, ctx.now(), [])
    # Access に阻まれるとログイン画面の HTML が返り、get_json が例外にする
    r = ctx.http.get_json(fc.signage.url, headers={
        "CF-Access-Client-Id": ctx.secret("OPS_SIGNAGE_ACCESS_CLIENT_ID"),
        "CF-Access-Client-Secret": ctx.secret("OPS_SIGNAGE_ACCESS_CLIENT_SECRET")})
    try:
        conns = {o: r["connections"][o] for o in _ORIGINS}
        at = r["at"]
    except (KeyError, TypeError):
        raise ValueError("unexpected signage stats response") from None
    if not all(type(v) is int for v in conns.values()):
        raise ValueError("unexpected signage stats response")
    return make_result(SOURCE_ID, ctx.now(), [Item("connections", "connections", Status.OK, {**conns, "at": at})])


SOURCES = (Source(SOURCE_ID, timedelta(seconds=60), fetch),)
