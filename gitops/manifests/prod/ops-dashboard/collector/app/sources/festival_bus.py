from datetime import datetime, timedelta, timezone

from http_client import HttpError
from model import Item, Source, Status, iso, make_result

SOURCE_ID = "festival.bus"
_FEEDS = ("trip_update", "vehicle")


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        b = buf[i]  # 範囲外は IndexError
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(buf: bytes):
    """(field 番号, wire type, 値) を順に返す。値は varint なら int、length-delimited なら bytes。"""
    i = 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        num, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            n, i = _varint(buf, i)
            if i + n > len(buf):
                raise ValueError("truncated field")
            v, i = buf[i:i + n], i + n
        elif wt == 1:
            v, i = None, i + 8
        elif wt == 5:
            v, i = None, i + 4
        else:
            raise ValueError("unsupported wire type")
        yield num, wt, v


def read_header_timestamp(feed: bytes) -> int:
    """FeedMessage.header.timestamp (epoch 秒) を返す。見つからなければ ValueError。"""
    try:
        for num, wt, header in _fields(feed):
            if num == 1 and wt == 2:
                for n, w, v in _fields(header):
                    if n == 3 and w == 0:
                        return v
                break
    except IndexError:
        pass
    raise ValueError("feed has no header timestamp")


def fetch(ctx):
    fc = ctx.config.festival
    if fc is None or SOURCE_ID in fc.disabled_sources:
        return make_result(SOURCE_ID, ctx.now(), [])
    items, errors = [], []
    for name in _FEEDS:
        key = f"feed.{name}"
        try:
            ts = read_header_timestamp(ctx.http.get_bytes(fc.bus_feed_base + name))
            items.append(Item(key, key, Status.OK,
                              {"header_timestamp": iso(datetime.fromtimestamp(ts, timezone.utc))}))
        except (HttpError, ValueError, OverflowError) as e:
            errors.append(e)
            items.append(Item(key, key, Status.STALE, {"header_timestamp": None}, f"{type(e).__name__}: {e}"[:200]))
    if len(errors) == len(_FEEDS):
        raise RuntimeError(f"all bus feeds failed: {errors[0]}")
    return make_result(SOURCE_ID, ctx.now(), items, status=Status.OK if not errors else Status.STALE)


SOURCES = (Source(SOURCE_ID, timedelta(seconds=30), fetch),)
