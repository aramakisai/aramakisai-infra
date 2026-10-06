import hmac
import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs

FAIL2BAN_DB = os.environ.get("FAIL2BAN_DB", "/var/mail-state/lib-fail2ban/fail2ban.sqlite3")
SPOOL_DIR = os.environ.get("SPOOL_DIR", "/var/mail-state/spool-postfix")
MAIL_LOG = os.environ.get("MAIL_LOG", "/var/log/mail/mail.log")
REPORTS_DIR = os.environ.get("REPORTS_DIR", "/var/mail/aramakisai.com/ops-reports")
MAX_LINES = 5000
STATUS_RE = re.compile(rb"status=(deferred|bounced)\b")


class Unavailable(Exception):
    pass


def fail2ban_bans(db=None):
    try:
        # mode=ro: DB への書き込みを SQLite 側でも拒否する (immutable は使わない。稼働中 DB の WAL を読むため)
        con = sqlite3.connect(f"file:{db or FAIL2BAN_DB}?mode=ro", uri=True, timeout=2)
        try:
            rows = con.execute("SELECT jail, ip, timeofban, bantime, bancount FROM bips").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        raise Unavailable(str(e))
    return {"bans": [dict(zip(("jail", "ip", "timeofban", "bantime", "bancount"), r)) for r in rows]}


def queue_status(spool=None):
    spool = spool or SPOOL_DIR
    out = {}
    oldest = None
    try:
        for q in ("incoming", "active", "deferred", "hold"):
            n = 0
            for root, _, files in os.walk(os.path.join(spool, q), onerror=_raise):
                n += len(files)
                if q == "deferred":
                    for f in files:
                        m = os.stat(os.path.join(root, f)).st_mtime
                        oldest = m if oldest is None else min(oldest, m)
            out[q] = n
    except OSError as e:
        raise Unavailable(str(e))
    out["oldest_deferred_mtime"] = oldest
    return out


def _raise(e):
    raise e


def _read_matching(path, offset, limit):
    """offset 以降の完結した行から対象行を最大 limit 件返し、消費したバイト位置も返す。"""
    lines = []
    with open(path, "rb") as f:
        f.seek(offset)
        pos = offset
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # 書き込み途中の行は次回に回す
            pos += len(raw)
            if STATUS_RE.search(raw):
                lines.append(raw.decode("utf-8", "replace").rstrip("\n"))
                if len(lines) >= limit:
                    break
    return lines, pos


def maillog(cursor, path=None):
    path = path or MAIL_LOG
    try:
        cur_ino = os.stat(path).st_ino
        ino, off = cur_ino, 0
        if cursor:
            m = re.fullmatch(r"(\d+):(\d+)", cursor)
            if not m:
                raise ValueError("bad cursor")
            ino, off = int(m.group(1)), int(m.group(2))
        if ino != cur_ino:
            # ローテーション済み: 旧ファイル (.1) の残りを先に返し、次回から新ファイルの先頭へ進む
            rotated = path + ".1"
            if os.path.exists(rotated) and os.stat(rotated).st_ino == ino:
                lines, pos = _read_matching(rotated, off, MAX_LINES)
                if pos < os.stat(rotated).st_size:
                    return {"cursor": f"{ino}:{pos}", "lines": lines}
                return {"cursor": f"{cur_ino}:0", "lines": lines}
            ino, off = cur_ino, 0
        if off > os.stat(path).st_size:
            # docker-mailserver は copytruncate でローテーションするため inode は変わらない。
            # 切り詰め前の末尾は .1 (コピー) にだけ残っているので、先にそこから読んで新ファイルの先頭へ進む。
            # ponytail: 取りこぼし上限件数に達した場合は .1 の途中位置を返すため、その間に mail.log が伸びると位置を取り違える
            rotated = path + ".1"
            if os.path.exists(rotated) and os.stat(rotated).st_size >= off:
                lines, pos = _read_matching(rotated, off, MAX_LINES)
                if pos < os.stat(rotated).st_size:
                    return {"cursor": f"{cur_ino}:{pos}", "lines": lines}
                return {"cursor": f"{cur_ino}:0", "lines": lines}
            off = 0
        lines, pos = _read_matching(path, off, MAX_LINES)
        return {"cursor": f"{cur_ino}:{pos}", "lines": lines}
    except OSError as e:
        raise Unavailable(str(e))


def _maildir_files(root):
    for sub in ("new", "cur"):
        d = os.path.join(root, sub)
        try:
            for name in os.listdir(d):
                yield name, os.path.join(d, name)
        except FileNotFoundError:
            continue


def report_keys(root=None):
    root = root or REPORTS_DIR
    if not os.path.isdir(root):
        raise Unavailable("reports dir missing")
    try:
        keys = []
        for name, p in _maildir_files(root):
            st = os.stat(p)
            keys.append({"key": name, "mtime": st.st_mtime, "size": st.st_size})
    except OSError as e:
        raise Unavailable(str(e))
    return {"keys": sorted(keys, key=lambda k: k["key"])}


def report_body(key, root=None):
    root = root or REPORTS_DIR
    if not key or "/" in key or key.startswith("."):
        return None
    try:
        for name, p in _maildir_files(root):
            if name == key:
                with open(p, "rb") as f:
                    return f.read()
    except OSError as e:
        raise Unavailable(str(e))
    return None


def make_handler(token):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # トークン未設定なら空文字との一致を許さず全拒否
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {token}" if token else None
            if expected is None or not hmac.compare_digest(auth.encode(), expected.encode()):
                return self._send(401, {"error": "unauthorized"})
            u = urlsplit(self.path)
            try:
                if u.path == "/fail2ban":
                    return self._send(200, fail2ban_bans())
                if u.path == "/queue":
                    return self._send(200, queue_status())
                if u.path == "/maillog":
                    cur = parse_qs(u.query).get("cursor", [""])[0]
                    try:
                        return self._send(200, maillog(cur))
                    except ValueError:
                        return self._send(400, {"error": "bad cursor"})
                if u.path == "/reports":
                    return self._send(200, report_keys())
                if u.path.startswith("/reports/"):
                    body = report_body(unquote(u.path[len("/reports/"):]))
                    if body is None:
                        return self._send(404, {"error": "not found"})
                    return self._send(200, body, "message/rfc822")
            except Unavailable as e:
                print(f"unavailable {u.path}: {e}", flush=True)
                return self._send(503, {"error": "unavailable"})
            return self._send(404, {"error": "not found"})

        def log_message(self, *a):
            pass

    return H


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))),
                        make_handler(os.environ.get("OPS_MAIL_AGENT_TOKEN", ""))).serve_forever()
