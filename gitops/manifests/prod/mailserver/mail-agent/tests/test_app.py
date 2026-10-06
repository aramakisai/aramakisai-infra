import shutil
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import app  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.root = self.d.name

    def p(self, *a):
        return os.path.join(self.root, *a)


class Fail2ban(Base):
    def test_bans_and_ro(self):
        db = self.p("f.sqlite3")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE bips (jail TEXT, ip TEXT, timeofban INT, bantime INT, bancount INT, data TEXT)")
        c.execute("INSERT INTO bips VALUES ('postfix','1.2.3.4',100,600,2,'{}')")
        c.commit()
        c.close()
        self.assertEqual(app.fail2ban_bans(db)["bans"],
                         [{"jail": "postfix", "ip": "1.2.3.4", "timeofban": 100, "bantime": 600, "bancount": 2}])
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("DELETE FROM bips")

    def test_missing_db(self):
        with self.assertRaises(app.Unavailable):
            app.fail2ban_bans(self.p("none.sqlite3"))


class Queue(Base):
    def test_counts(self):
        for q, n in (("incoming", 1), ("active", 0), ("deferred", 2), ("hold", 0)):
            os.makedirs(self.p(q, "A"))
            for i in range(n):
                open(self.p(q, "A", f"id{i}"), "w").close()
        os.utime(self.p("deferred", "A", "id0"), (500, 500))
        os.utime(self.p("deferred", "A", "id1"), (900, 900))
        r = app.queue_status(self.root)
        self.assertEqual(r, {"incoming": 1, "active": 0, "deferred": 2, "hold": 0, "oldest_deferred_mtime": 500})

    def test_empty_oldest_null_and_missing(self):
        for q in ("incoming", "active", "deferred", "hold"):
            os.makedirs(self.p(q))
        self.assertIsNone(app.queue_status(self.root)["oldest_deferred_mtime"])
        with self.assertRaises(app.Unavailable):
            app.queue_status(self.p("nope"))


class Maillog(Base):
    def setUp(self):
        super().setUp()
        self.log = self.p("mail.log")

    def w(self, path, text, mode="w"):
        with open(path, mode) as f:
            f.write(text)

    def test_filter_and_advance(self):
        self.w(self.log, "a status=sent\nb status=deferred (x)\nc status=bounced\npartial status=deferred")
        r = app.maillog("", self.log)
        self.assertEqual(r["lines"], ["b status=deferred (x)", "c status=bounced"])
        self.w(self.log, "\nd status=deferred\n", "a")
        r2 = app.maillog(r["cursor"], self.log)
        self.assertEqual(r2["lines"], ["partial status=deferred", "d status=deferred"])
        self.assertEqual(app.maillog(r2["cursor"], self.log)["lines"], [])

    def test_rotation(self):
        self.w(self.log, "a status=deferred\n")
        r = app.maillog("", self.log)
        self.w(self.log, "b status=bounced\n", "a")
        os.rename(self.log, self.log + ".1")
        self.w(self.log, "c status=deferred\n")
        r2 = app.maillog(r["cursor"], self.log)
        self.assertEqual(r2["lines"], ["b status=bounced"])
        r3 = app.maillog(r2["cursor"], self.log)
        self.assertEqual(r3["lines"], ["c status=deferred"])

    def test_copytruncate_rotation_reads_tail_from_copy(self):
        self.w(self.log, "a status=deferred\nxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n")
        r = app.maillog("", self.log)
        self.w(self.log, "b status=bounced\n", "a")
        shutil.copyfile(self.log, self.log + ".1")
        open(self.log, "w").close()
        self.w(self.log, "c status=deferred\n")
        r2 = app.maillog(r["cursor"], self.log)
        self.assertEqual(r2["lines"], ["b status=bounced"])
        r3 = app.maillog(r2["cursor"], self.log)
        self.assertEqual(r3["lines"], ["c status=deferred"])

    def test_bad_cursor_and_missing(self):
        self.w(self.log, "")
        with self.assertRaises(ValueError):
            app.maillog("zzz", self.log)
        with self.assertRaises(app.Unavailable):
            app.maillog("", self.p("none"))

    def test_unknown_inode_restarts(self):
        self.w(self.log, "a status=deferred\n")
        self.assertEqual(app.maillog("1:5", self.log)["lines"], ["a status=deferred"])


class Reports(Base):
    def setUp(self):
        super().setUp()
        for s in ("new", "cur"):
            os.makedirs(self.p(s))
        open(self.p("new", "m1"), "wb").write(b"raw1")
        open(self.p("cur", "m2:2,S"), "wb").write(b"raw2")

    def test_list_and_body(self):
        r = app.report_keys(self.root)
        self.assertEqual([k["key"] for k in r["keys"]], ["m1", "m2:2,S"])
        self.assertEqual(r["keys"][0]["size"], 4)
        self.assertEqual(app.report_body("m2:2,S", self.root), b"raw2")

    def test_traversal_and_missing(self):
        self.assertIsNone(app.report_body("../x", self.root))
        self.assertIsNone(app.report_body("..", self.root))
        self.assertIsNone(app.report_body("nope", self.root))
        with self.assertRaises(app.Unavailable):
            app.report_keys(self.p("none"))


class Http(Base):
    def serve(self, token):
        s = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(token))
        threading.Thread(target=s.serve_forever, daemon=True).start()
        self.addCleanup(s.server_close)
        self.addCleanup(s.shutdown)
        return s.server_address[1]

    def get(self, port, path, auth=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if auth is not None:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_auth_required(self):
        port = self.serve("secret")
        for path in ("/fail2ban", "/queue", "/maillog", "/reports", "/reports/x"):
            self.assertEqual(self.get(port, path)[0], 401)
            self.assertEqual(self.get(port, path, "Bearer wrong")[0], 401)
            self.assertEqual(self.get(port, path, "secret")[0], 401)

    def test_unset_token_denies_all(self):
        port = self.serve("")
        self.assertEqual(self.get(port, "/queue", "Bearer ")[0], 401)
        self.assertEqual(self.get(port, "/queue", "Bearer")[0], 401)
        self.assertEqual(self.get(port, "/queue")[0], 401)

    def test_routes(self):
        port = self.serve("t")
        a = "Bearer t"
        os.makedirs(self.p("new"))
        open(self.p("new", "k"), "wb").write(b"body")
        app.REPORTS_DIR = self.root
        app.MAIL_LOG = self.p("none.log")
        app.SPOOL_DIR = self.p("nospool")
        app.FAIL2BAN_DB = self.p("none.db")
        code, body, h = self.get(port, "/reports/k", a)
        self.assertEqual((code, body, h["Content-Type"]), (200, b"body", "message/rfc822"))
        self.assertEqual(self.get(port, "/reports/zz", a)[0], 404)
        self.assertEqual(self.get(port, "/reports", a)[0], 200)
        self.assertEqual(self.get(port, "/nothing", a)[0], 404)
        for path in ("/fail2ban", "/queue", "/maillog"):
            self.assertEqual(self.get(port, path, a)[0], 503)
        open(app.MAIL_LOG, "w").write("")
        self.assertEqual(self.get(port, "/maillog?cursor=bad", a)[0], 400)
        code, body, _ = self.get(port, "/maillog", a)
        self.assertEqual(json.loads(body)["lines"], [])


if __name__ == "__main__":
    unittest.main()
