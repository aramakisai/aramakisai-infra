import hashlib
import hmac
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timedelta, timezone

from model import Item, Source, Status, make_result
from plan import jst_today, plan_item, quota_item


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sigv4_headers(host, path, params, access_key, secret_key, region, now):
    """S3 ListObjectsV2 (GET・本文なし) 用の AWS Signature V4 ヘッダ。"""
    amz_date = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    payload = hashlib.sha256(b"").hexdigest()
    query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                     for k, v in sorted(params.items()))
    signed = "host;x-amz-content-sha256;x-amz-date"
    canonical = "\n".join(["GET", urllib.parse.quote(path, safe="/-_.~"), query,
                           f"host:{host}\nx-amz-content-sha256:{payload}\nx-amz-date:{amz_date}\n", signed, payload])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(("AWS4" + secret_key).encode(), day), region), "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"x-amz-date": amz_date, "x-amz-content-sha256": payload,
            "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"}


def bucket_bytes(ctx, endpoint, bucket, region, access_key, secret_key):
    host = urllib.parse.urlsplit(endpoint).netloc
    total, token = 0, None
    while True:
        params = {"list-type": "2"}
        if token:
            params["continuation-token"] = token
        hdr = sigv4_headers(host, f"/{bucket}", params, access_key, secret_key, region, ctx.now())
        root = ET.fromstring(ctx.http.get_bytes(f"{endpoint}/{bucket}", params=params, headers=hdr))
        ns = {"s": root.tag.partition("}")[0].lstrip("{")}
        q = (lambda t: f"s:{t}") if ns["s"] else (lambda t: t)
        total += sum(int(e.text) for e in root.findall(f"{q('Contents')}/{q('Size')}", ns))
        if (root.findtext(q("IsTruncated"), namespaces=ns) or "").lower() != "true":
            return total
        token = root.findtext(q("NextContinuationToken"), namespaces=ns)


def fetch(ctx):
    ak, sk = ctx.secret("HETZNER_OS_ACCESS_KEY_ID"), ctx.secret("HETZNER_OS_SECRET_ACCESS_KEY")
    st = ctx.config.source_settings("billing.hetzner_os")
    endpoint = st["endpoint"].rstrip("/")
    region = st.get("region") or urllib.parse.urlsplit(endpoint).hostname.split(".")[0]
    now = ctx.now()
    sizes = {b: bucket_bytes(ctx, endpoint, b, region, ak, sk) for b in st["buckets"]}

    plan = ctx.config.active_plan("hetzner_object_storage", jst_today(now))
    items = [plan_item(ctx.config, "hetzner_object_storage", now)]
    included = plan.limits.get("storage_gb") if plan else None
    total_gb = sum(sizes.values()) / 1e9
    items.append(quota_item("os.total", "sub.object_storage", round(total_gb, 3), included, "GB",
                            ctx.config.thresholds.warn_ratio))
    items += [Item(f"os.bucket.{b}", b, Status.OK, {"bytes": n}) for b, n in sizes.items()]
    return make_result("billing.hetzner_os", now, items)


SOURCES = (Source("billing.hetzner_os", timedelta(hours=6), fetch),)
