"""mail-agent から受け取った生メッセージから DMARC (XML) と TLS-RPT (JSON) を取り出して保存する。"""
import email
import io
import json
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import datetime, timezone
from email import policy

import model

# 展開後の上限。圧縮爆弾でメモリ上限 (160Mi) を超えないようにする。
MAX_BYTES = 5_000_000
_MAX_MEMBERS = 20
_MAX_DEPTH = 3


def _gunzip(data: bytes) -> bytes:
    d = zlib.decompressobj(31)
    out = d.decompress(data, MAX_BYTES + 1)
    if len(out) > MAX_BYTES:
        raise ValueError("decompressed size exceeds limit")
    return out


def _unzip(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist()[:_MAX_MEMBERS]:
            if info.is_dir():
                continue
            if info.file_size > MAX_BYTES:
                raise ValueError("zip member exceeds limit")
            with z.open(info) as f:
                out = f.read(MAX_BYTES + 1)
            if len(out) > MAX_BYTES:
                raise ValueError("zip member exceeds limit")
            yield out


def _unpack(data: bytes, depth=0):
    """(kind, bytes) を返す。kind は xml / json。レポートに見えないものは捨てる。"""
    if depth > _MAX_DEPTH:
        raise ValueError("nesting too deep")
    if data.startswith(b"PK\x03\x04"):
        for m in _unzip(data):
            yield from _unpack(m, depth + 1)
    elif data.startswith(b"\x1f\x8b"):
        yield from _unpack(_gunzip(data), depth + 1)
    else:
        head = data.lstrip(b"\xef\xbb\xbf \r\n\t")
        # HTML 本文など報告書でない添付を失敗として数えないよう、目印で絞る。
        if head.startswith(b"<") and b"<feedback" in data[:4096]:
            yield "xml", data
        elif head.startswith(b"{") and b'"organization-name"' in data:
            yield "json", data


def _epoch_iso(v) -> str:
    return model.iso(datetime.fromtimestamp(int(v), timezone.utc))


def _text(node, path, default=""):
    n = node.find(path)
    return (n.text or "").strip() if n is not None and n.text else default


def parse_dmarc(data: bytes) -> dict:
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("DOCTYPE is not allowed")
    root = ET.fromstring(data)
    org, rid = _text(root, "report_metadata/org_name"), _text(root, "report_metadata/report_id")
    if not org or not rid:
        raise ValueError("missing org_name or report_id")
    records = [{
        "source_ip": _text(r, "row/source_ip"), "count": int(_text(r, "row/count", "0")),
        "disposition": _text(r, "row/policy_evaluated/disposition"),
        "dkim": _text(r, "row/policy_evaluated/dkim"), "spf": _text(r, "row/policy_evaluated/spf"),
        "header_from": _text(r, "identifiers/header_from"),
    } for r in root.findall("record")]
    return {"org_name": org, "report_id": rid,
            "begin": _epoch_iso(_text(root, "report_metadata/date_range/begin")),
            "end": _epoch_iso(_text(root, "report_metadata/date_range/end")), "records": records}


def _iso_z(s: str) -> str:
    return model.iso(model.parse_iso(s))


def parse_tlsrpt(data: bytes) -> dict:
    d = json.loads(data)
    org, rid = d["organization-name"], d["report-id"]
    policies = d.get("policies") or []
    domains = sorted({p.get("policy", {}).get("policy-domain", "") for p in policies} - {""})
    return {"org_name": org, "report_id": rid,
            "begin": _iso_z(d["date-range"]["start-datetime"]), "end": _iso_z(d["date-range"]["end-datetime"]),
            "policy_domain": ",".join(domains),
            "success": sum(int(p["summary"].get("total-successful-session-count", 0)) for p in policies),
            "failure": sum(int(p["summary"].get("total-failure-session-count", 0)) for p in policies)}


def parse_message(raw: bytes) -> tuple[list[tuple[str, dict]], list[str]]:
    """(報告のリスト [(dmarc|tlsrpt, 内容)], 失敗理由のリスト)。解析できない添付があっても他は続ける。"""
    reports, errors = [], []
    msg = email.message_from_bytes(raw, policy=policy.default)
    for part in msg.walk():
        if part.is_multipart():
            continue
        try:
            data = part.get_payload(decode=True)
            if not data:
                continue
            for kind, body in _unpack(data):
                reports.append(("dmarc", parse_dmarc(body)) if kind == "xml" else ("tlsrpt", parse_tlsrpt(body)))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}"[:200])
    return reports, errors


def ingest_message(store, key: str, raw: bytes, now: datetime) -> str:
    """1 通を保存して種別 (dmarc / tlsrpt / other) を返す。解析失敗は ingest_failures に残し、メッセージは取り込み済みにする。"""
    reports, errors = parse_message(raw)
    kind = "dmarc" if any(k == "dmarc" for k, _ in reports) else "tlsrpt" if reports else "other"
    t = model.iso(now)

    def save(c):
        for k, r in reports:
            if k == "dmarc":
                cur = c.execute("INSERT OR IGNORE INTO dmarc_reports(org_name,report_id,begin,end) VALUES (?,?,?,?)",
                                (r["org_name"], r["report_id"], r["begin"], r["end"]))
                if cur.rowcount:
                    c.executemany(
                        "INSERT INTO dmarc_records(org_name,report_id,source_ip,count,disposition,dkim,spf,header_from)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        [(r["org_name"], r["report_id"], x["source_ip"], x["count"], x["disposition"], x["dkim"],
                          x["spf"], x["header_from"]) for x in r["records"]])
            else:
                c.execute("INSERT OR IGNORE INTO tlsrpt_reports(org_name,report_id,begin,end,policy_domain,success,"
                          "failure) VALUES (?,?,?,?,?,?,?)",
                          (r["org_name"], r["report_id"], r["begin"], r["end"], r["policy_domain"], r["success"],
                           r["failure"]))
        for reason in errors:
            c.execute("INSERT INTO ingest_failures(source,key,time,reason) VALUES ('mail.reports',?,?,?)",
                      (key, t, reason))
        c.execute("INSERT OR REPLACE INTO report_messages(key,kind,ingested_at) VALUES (?,?,?)", (key, kind, t))

    store.write(save)
    return kind
