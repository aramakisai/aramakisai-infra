"""mail-agent を本番の securityContext 相当 (root・cap_drop ALL + DAC_READ_SEARCH・ro マウント・read_only rootfs) の
コンテナで起動し、API 契約と書き込み拒否を確認する。さらに collector の mail 系情報源で取り込めることを確認する。

実行: python3 -m unittest discover -s tests/integration  (mail-agent ディレクトリで。docker が必要で sudo は不要)
フィクスチャの所有者設定はコンテナ内で行い、ホストには root 所有ファイルを作らない。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from dataclasses import replace

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.abspath(os.path.join(HERE, "..", "..", "app.py"))
COLLECTOR = os.path.abspath(os.path.join(HERE, "..", "..", "..", "..", "ops-dashboard", "collector", "app"))
sys.path[:0] = [COLLECTOR, os.path.join(COLLECTOR, "tests")]

import http_client  # noqa: E402
from helpers import FIXED_NOW, make_ctx, temp_store  # noqa: E402
from sources import mail as mail_src  # noqa: E402
from test_report_ingest import DMARC_XML, TLS, mail as mk_mail, zipped  # noqa: E402

IMAGE = "python:3.13-slim"
TOKEN = "integration-token"
POSTFIX_UID, VMAIL_UID = 100, 5000

D1 = ("2026-06-01T11:00:00.123456+00:00 mx postfix/smtp[1]: AB12: to=<alice@example.org>, relay=mx.example.org[192.0.2.1]:25, "
      "delay=5, dsn=4.4.1, status=deferred (connect to mx.example.org[192.0.2.1]:25: Connection timed out)")
B1 = ("2026-06-01T10:00:00+00:00 mx postfix/smtp[2]: CD34: to=<bob@example.net>, relay=none, delay=1, dsn=5.1.1, "
      "status=bounced (host mx.example.net said: 550 5.1.1 <bob@example.net>: Recipient address rejected)")
SENT = "2026-06-01T10:30:00+00:00 mx postfix/smtp[3]: EF56: to=<c@example.com>, status=sent (250 ok)"
PARTIAL = "2026-06-01T11:59:00+00:00 mx postfix/smtp[4]: GH78: to=<d@example.com>, status=bounced (half"


def docker(*args, check=True, input=None):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, input=input)
    if check and r.returncode:
        raise RuntimeError(f"docker {' '.join(args[:3])}: {r.stderr.strip()}")
    return r


def run_root(*mounts, script):
    """既定 capability の root で一度だけ sh を実行する (フィクスチャの配置・差し替え用)。"""
    args = ["run", "--rm"]
    for m in mounts:
        args += ["-v", m]
    docker(*args, IMAGE, "sh", "-ec", script)


def get(base, path, token=TOKEN):
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"} if token is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers.get("Content-Type"), e.read()


def agent_args(name, vols, token):
    a = ["run", "-d", "--name", name, "--user", "0", "--cap-drop", "ALL", "--cap-add", "DAC_READ_SEARCH",
         "--security-opt", "no-new-privileges", "--read-only", "-p", "127.0.0.1::8080",
         "-e", f"OPS_MAIL_AGENT_TOKEN={token}", "-e", "PYTHONDONTWRITEBYTECODE=1",
         "-v", f"{AGENT}:/app/app.py:ro"]
    for v, dest in vols.items():
        a += ["-v", f"{v}:{dest}:ro"]
    return a + [IMAGE, "python", "/app/app.py"]


class ContainerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            docker("version")
            docker("image", "inspect", IMAGE)
        except (RuntimeError, FileNotFoundError) as e:
            raise unittest.SkipTest(f"docker/{IMAGE} unavailable: {e}")
        tag = uuid.uuid4().hex[:8]
        cls.vols = {f"mailagent-{k}-{tag}": d for k, d in (
            ("state", "/var/mail-state"), ("logs", "/var/log/mail"),
            ("reports", "/var/mail/aramakisai.com/ops-reports"))}
        cls.state, cls.logs, cls.reports = cls.vols
        cls.containers = [f"mailagent-{tag}", f"mailagent-notoken-{tag}"]
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.cleanup)
        cls.build_fixture()
        cls.base = cls.start(cls.containers[0], TOKEN)
        cls.base_notoken = cls.start(cls.containers[1], "")

    @classmethod
    def cleanup(cls):
        for c in cls.containers:
            docker("rm", "-f", c, check=False)
        for v in cls.vols:
            docker("volume", "rm", "-f", v, check=False)
        cls.tmp.cleanup()

    @classmethod
    def start(cls, name, token):
        docker(*agent_args(name, cls.vols, token))
        port = docker("port", name, "8080/tcp").stdout.split(":")[-1].strip()
        base = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                get(base, "/queue", token=None)
                return base
            except OSError:
                time.sleep(0.2)
        raise RuntimeError(docker("logs", name, check=False).stderr)

    @classmethod
    def build_fixture(cls):
        src = cls.tmp.name
        t = FIXED_NOW.timestamp()
        for d in ("spool/incoming", "spool/active", "spool/deferred/0/A", "spool/hold", "f2b", "logs",
                  "reports/new", "reports/cur"):
            os.makedirs(os.path.join(src, d))
        for q, n in (("incoming", 1), ("active", 2), ("deferred/0/A", 3)):
            for i in range(n):
                p = os.path.join(src, "spool", q, f"Q{i}")
                open(p, "w").close()
                # 最古の deferred は 2 時間前
                os.utime(p, (t - 7200 + i, t - 7200 + i))
        db = os.path.join(src, "f2b", "fail2ban.sqlite3")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE bips (jail TEXT, ip TEXT, timeofban INT, bantime INT, bancount INT, data TEXT)")
        c.executemany("INSERT INTO bips VALUES (?,?,?,?,?,'{}')", [
            ("postfix", "203.0.113.5", int(t) - 100, 600, 2),
            ("dovecot", "203.0.113.6", int(t) - 9000, 600, 1),
            ("postfix", "203.0.113.7", int(t) - 50, -1, 4)])
        c.commit()
        c.close()
        with open(os.path.join(src, "logs", "mail.log"), "w") as f:
            f.write("\n".join([D1, SENT, B1]) + "\n" + PARTIAL)
        files = {
            "new/1780000001.M1.mx,S=1:2,": mk_mail(zipped(DMARC_XML), "r.zip", ("application", "zip")),
            "cur/1780000002.M2.mx,S=2:2,S": mk_mail(json.dumps(TLS).encode(), "r.json", ("application", "json")),
            "cur/1780000003.M3.mx,S=3:2,S": b"Subject: hello\r\n\r\nplain\r\n"}
        for name, raw in files.items():
            with open(os.path.join(src, "reports", name), "wb") as f:
                f.write(raw)
        run_root(f"{src}:/src:ro", f"{cls.state}:/s", f"{cls.logs}:/l", f"{cls.reports}:/r", script=f"""
            mkdir -p /s/lib-fail2ban
            cp -a /src/spool /s/spool-postfix
            cp -a /src/f2b/fail2ban.sqlite3 /s/lib-fail2ban/
            cp -a /src/logs/mail.log /l/
            cp -a /src/reports/. /r/
            chown -R {POSTFIX_UID}:{POSTFIX_UID} /s/spool-postfix
            chmod -R go-rwx /s/spool-postfix
            chown -R {VMAIL_UID}:{VMAIL_UID} /r
            chmod -R go-rwx /r
            chown -R 0:0 /s/lib-fail2ban /l
            chmod 600 /s/lib-fail2ban/fail2ban.sqlite3 /l/mail.log
        """)

    def j(self, path, **kw):
        code, _, body = get(self.base, path, **kw)
        return code, json.loads(body)


class Contract(ContainerTest):
    def test_auth(self):
        for tok in (None, "", "wrong"):
            self.assertEqual(get(self.base, "/queue", token=tok)[0], 401)
        for p in ("/queue", "/fail2ban", "/maillog", "/reports", "/reports/x", "/nope"):
            self.assertEqual(get(self.base_notoken, p, token="")[0], 401, p)

    def test_unknown_path(self):
        self.assertEqual(self.j("/nope")[0], 404)

    def test_runs_with_production_conditions(self):
        out = docker("exec", self.containers[0], "sh", "-c", "id -u; grep -E '^Cap(Eff|Bnd)' /proc/self/status").stdout
        self.assertIn("0\n", out)
        # DAC_READ_SEARCH は bit 2
        self.assertEqual([l.split()[1] for l in out.splitlines() if l.startswith("Cap")], ["0000000000000004"] * 2)

    def test_fixture_unreadable_without_capability(self):
        r = docker("run", "--rm", "--user", "0", "--cap-drop", "ALL", "-v", f"{self.state}:/s:ro", IMAGE,
                   "ls", "/s/spool-postfix/incoming", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Permission denied", r.stderr)

    def test_fail2ban(self):
        code, body = self.j("/fail2ban")
        self.assertEqual(code, 200)
        t = int(FIXED_NOW.timestamp())
        self.assertEqual(sorted(body["bans"], key=lambda b: b["ip"]), [
            {"jail": "postfix", "ip": "203.0.113.5", "timeofban": t - 100, "bantime": 600, "bancount": 2},
            {"jail": "dovecot", "ip": "203.0.113.6", "timeofban": t - 9000, "bantime": 600, "bancount": 1},
            {"jail": "postfix", "ip": "203.0.113.7", "timeofban": t - 50, "bantime": -1, "bancount": 4}])

    def test_queue(self):
        code, body = self.j("/queue")
        self.assertEqual(code, 200)
        self.assertEqual(body, {"incoming": 1, "active": 2, "deferred": 3, "hold": 0,
                                "oldest_deferred_mtime": FIXED_NOW.timestamp() - 7200})

    def test_maillog_from_start_skips_non_matching_and_partial_line(self):
        code, body = self.j("/maillog")
        self.assertEqual(code, 200)
        self.assertEqual(body["lines"], [D1, B1])
        size = len(("\n".join([D1, SENT, B1]) + "\n").encode())
        self.assertTrue(body["cursor"].endswith(f":{size}"))
        self.assertEqual(self.j(f"/maillog?cursor={body['cursor']}")[1], {"cursor": body["cursor"], "lines": []})

    def test_maillog_bad_cursor(self):
        for c in ("abc", "1:", "1:2:3", "-1:0"):
            self.assertEqual(self.j(f"/maillog?cursor={c}")[0], 400, c)

    def test_maillog_unknown_inode_and_oversize_offset_restart(self):
        for c in ("1:0", f"1:99999999"):
            self.assertEqual(self.j(f"/maillog?cursor={c}")[1]["lines"], [D1, B1], c)
        ino = self.j("/maillog")[1]["cursor"].split(":")[0]
        self.assertEqual(self.j(f"/maillog?cursor={ino}:99999999")[1]["lines"], [D1, B1])

    def test_reports(self):
        code, body = self.j("/reports")
        self.assertEqual(code, 200)
        keys = [k["key"] for k in body["keys"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(keys), 3)
        self.assertTrue(all(k["size"] > 0 and k["mtime"] > 0 for k in body["keys"]))
        code, ctype, raw = get(self.base, "/reports/1780000003.M3.mx,S=3:2,S")
        self.assertEqual((code, ctype, raw), (200, "message/rfc822", b"Subject: hello\r\n\r\nplain\r\n"))

    def test_report_body_not_found(self):
        for k in ("missing", "..%2F..%2Fetc%2Fpasswd", "cur%2F1780000002.M2.mx,S=2:2,S", ".hidden", ".."):
            self.assertEqual(get(self.base, f"/reports/{k}")[0], 404, k)

    def test_writes_are_rejected(self):
        script = r"""
import os, sqlite3, sys
bad = []
def attempt(label, fn):
    try:
        fn()
    except (OSError, sqlite3.Error):
        return
    bad.append(label)
attempt("rootfs", lambda: open("/x", "w"))
attempt("tmp", lambda: open("/tmp/x", "w"))
attempt("spool create", lambda: open("/var/mail-state/spool-postfix/deferred/x", "w"))
attempt("spool unlink", lambda: os.unlink("/var/mail-state/spool-postfix/incoming/Q0"))
attempt("log append", lambda: open("/var/log/mail/mail.log", "a"))
attempt("maildir create", lambda: open("/var/mail/aramakisai.com/ops-reports/new/x", "w"))
attempt("maildir unlink", lambda: os.unlink("/var/mail/aramakisai.com/ops-reports/new/1780000001.M1.mx,S=1:2,"))
attempt("db write", lambda: sqlite3.connect("file:/var/mail-state/lib-fail2ban/fail2ban.sqlite3?mode=ro", uri=True).execute("DELETE FROM bips"))
attempt("db rw open", lambda: sqlite3.connect("file:/var/mail-state/lib-fail2ban/fail2ban.sqlite3?mode=rw", uri=True).execute("DELETE FROM bips"))
attempt("chmod", lambda: os.chmod("/var/log/mail/mail.log", 0o666))
sys.exit("writable: " + ", ".join(bad) if bad else 0)
"""
        r = docker("exec", self.containers[0], "python", "-c", script, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        # 拒否された後もデータは無傷
        self.assertEqual(self.j("/queue")[1]["incoming"], 1)
        self.assertEqual(len(self.j("/fail2ban")[1]["bans"]), 3)


class WithCollector(ContainerTest):
    def setUp(self):
        self.d, self.st = temp_store()
        self.addCleanup(self.d.cleanup)
        self.addCleanup(self.st.close)
        cfg = make_ctx().config
        cfg = replace(cfg, sources={**cfg.sources, "mail.delivery": {"url": self.base}})
        self.ctx = make_ctx(cfg=cfg, http=http_client.Http(), env={"OPS_MAIL_AGENT_TOKEN": TOKEN}, store=self.st)

    def fetch(self, sid):
        return next(s for s in mail_src.SOURCES if s.source_id == sid).fetch(self.ctx)

    def test_delivery_failures(self):
        res = self.fetch("mail.delivery")
        by = {i.key: i for i in res.items}
        self.assertEqual(by["mail.queue"].values["deferred"], 3)
        self.assertEqual(by["mail.queue"].values["oldest_deferred_age_s"], 7200)
        self.assertEqual(by["mail.series.2026-06-01"].values, {"deferred": 1, "bounced": 1})
        self.assertEqual({(i.values["domain"]) for i in res.items if i.key.startswith("mail.top.")},
                         {"example.org", "example.net"})
        # 受信者アドレスは保存しない
        self.assertFalse(any("alice@" in str(r[0]) or "bob@" in str(r[0]) for r in self.st.query(
            "SELECT reason FROM mail_events")))
        self.fetch("mail.delivery")
        self.assertEqual(self.st.query("SELECT count(*) FROM mail_events")[0][0], 2)

    def test_fail2ban_bans_exclude_expired(self):
        res = self.fetch("mail.fail2ban")
        self.assertEqual(sorted(i.key for i in res.items), ["ban.postfix.203.0.113.5", "ban.postfix.203.0.113.7"])
        self.assertIsNone(next(i for i in res.items if i.key.endswith(".7")).values["until"])

    def test_dmarc_and_tlsrpt(self):
        res = self.fetch("mail.reports")
        self.assertIsNone(res.error)
        q = self.st.query
        self.assertEqual(q("SELECT count(*) FROM dmarc_reports")[0][0], 1)
        self.assertEqual(q("SELECT sum(count) FROM dmarc_records")[0][0], 5)
        self.assertEqual(tuple(q("SELECT success, failure FROM tlsrpt_reports")[0]), (15, 2))
        self.assertEqual(q("SELECT count(*) FROM ingest_failures")[0][0], 0)
        self.assertEqual(q("SELECT count(*) FROM report_messages")[0][0], 3)
        keys = {i.key for i in res.items}
        self.assertIn("dmarc.day.2026-06-01", keys)
        self.assertIn("tls.day.2026-06-01", keys)


class ZRotation(ContainerTest):
    """mail.log を差し替えるため、他のテストの後 (クラス名順) に実行する。"""

    def test_rotation_reads_rest_of_old_file_then_new_file(self):
        code, body = self.j("/maillog")
        old = body["cursor"]
        late = "2026-06-01T11:59:30+00:00 mx postfix/smtp[5]: IJ90: to=<e@example.com>, status=deferred (late)"
        new = "2026-06-02T00:00:01+00:00 mx postfix/smtp[6]: KL12: to=<f@example.com>, status=bounced (fresh)"
        run_root(f"{self.logs}:/l", script=f"""
            printf ')\\n%s\\n' '{late}' >> /l/mail.log
            mv /l/mail.log /l/mail.log.1
            printf '%s\\n' '{new}' > /l/mail.log
            chmod 600 /l/mail.log /l/mail.log.1
        """)
        code, body = self.j(f"/maillog?cursor={old}")
        # 末尾が改行なしだった行は、続きが書かれて完結したので今回返る
        self.assertEqual(body["lines"], [PARTIAL + ")", late])
        new_ino = body["cursor"].split(":")[0]
        self.assertTrue(body["cursor"].endswith(":0"))
        self.assertNotEqual(new_ino, old.split(":")[0])
        code, body2 = self.j(f"/maillog?cursor={body['cursor']}")
        self.assertEqual(body2["lines"], [new])
        self.assertEqual(self.j(f"/maillog?cursor={body2['cursor']}")[1]["lines"], [])


if __name__ == "__main__":
    unittest.main()
