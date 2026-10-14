"""portal の認可ルーティング統合テスト (docker compose + mock OIDC)。
実行: python3 -m unittest discover -s <このディレクトリ>
本番の nginx.conf・pages・generate-config.sh をそのまま使う。
"""
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.path.dirname(HERE)
BASE = "http://localhost:18080"
MOCK = "http://localhost:18081"
WORK = None
ENV = None


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


def compose(*args):
    r = subprocess.run(
        ["docker", "compose", "-p", "portal-authz-test", "-f", os.path.join(HERE, "compose.yaml"), *args],
        env=ENV, capture_output=True, text=True,
    )
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r


def setUpModule():
    global WORK, ENV
    WORK = tempfile.mkdtemp(prefix="portal-authz-")
    os.chmod(WORK, 0o755)
    # クラスタ DNS の代わりに docker の組み込み DNS を使う。それ以外は本番のまま
    with open(os.path.join(PORTAL, "nginx.conf")) as f:
        conf = f.read()
    conf = conf.replace("kube-dns.kube-system.svc.cluster.local", "127.0.0.11")
    write(os.path.join(WORK, "nginx.conf"), conf)
    site = os.path.join(WORK, "site")
    os.makedirs(os.path.join(site, "assets"))
    write(os.path.join(site, "index.html"), "HOMER-STUB")
    write(os.path.join(site, "assets", "config.yml.dist"), "dist")
    write(os.path.join(site, "sw.js"), "SW-STUB")
    write(os.path.join(site, "registerSW.js"), "REG-STUB")
    write(os.path.join(site, "logo.png"), "LOGO-STUB")
    write(os.path.join(site, "assets", "manifest.json"), "{}")
    for d, f in (("resources", "font.woff2"), ("assets/icons", "icon.png"), ("assets/themes", "theme.css")):
        os.makedirs(os.path.join(site, d))
        write(os.path.join(site, d, f), "STATIC-STUB")
    # 本番の生成スクリプトを実行する (yq は docker 上)
    subprocess.run(
        ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "sh",
         "-e", "PORTAL_NOTION_URL=https://notion.example/n",
         "-v", f"{PORTAL}:/portal:ro", "-v", f"{site}/assets:/out", "mikefarah/yq:4.54.1",
         "/portal/generate-config.sh", "/portal", "/out"],
        check=True, capture_output=True,
    )
    for root, dirs, files in os.walk(site):
        for n in dirs:
            os.chmod(os.path.join(root, n), 0o755)
        for n in files:
            os.chmod(os.path.join(root, n), 0o644)
    ENV = {**os.environ, "T_WORK": WORK, "T_PORTAL": PORTAL, "T_COLLECTOR_CONF": os.path.join(HERE, "collector.conf")}
    try:
        compose("up", "-d", "--quiet-pull")
        # oauth2-proxy の discovery 完了まで待つ
        for _ in range(60):
            try:
                urllib.request.urlopen(BASE + "/healthz", timeout=2)
                return
            except Exception:
                time.sleep(1)
        raise RuntimeError("portal が起動しない: " + compose("logs").stdout)
    except BaseException:
        tearDownModule()
        raise


def tearDownModule():
    try:
        compose("down", "-v")
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    """リダイレクトを追わない最小のブラウザ。Cookie だけ保持する。"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, url, data=None, headers=None):
        if url.startswith("/"):
            url = BASE + url
        url = url.replace("http://mock:8080", MOCK)
        try:
            r = self.op.open(urllib.request.Request(url, data=data, headers=headers or {}), timeout=10)
        except urllib.error.HTTPError as e:
            r = e
        r.body = r.read().decode()
        return r

    def start(self):
        r = self.req("/oauth2/start?rd=/")
        self.assertion(r.status in (302, 303), "start")
        return r.headers["Location"]

    def login(self, groups):
        self.finish(self.start(), groups)

    def finish(self, auth, groups):
        """authorize から callback まで進め、callback の応答を返す。"""
        claims = {"email": "u@example.com", "name": "u"}
        if groups is not None:
            claims["groups"] = groups
        form = urllib.parse.urlencode({"username": "u", "claims": json.dumps(claims)}).encode()
        r = self.req(auth, form)
        self.assertion(r.status in (302, 303), f"authorize {r.status} {r.body[:200]}")
        r = self.req(r.headers["Location"])
        self.assertion(r.status in (302, 303), f"callback {r.status} {r.body[:200]}")
        return r

    @staticmethod
    def assertion(ok, msg):
        if not ok:
            raise AssertionError(msg)


def session(groups):
    c = Client()
    c.login(groups)
    return c


class Authz(unittest.TestCase):
    def check_executive(self, c):
        r = c.req("/")
        self.assertEqual((r.status, r.body), (200, "HOMER-STUB"))
        r = c.req("/assets/config.yml")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "private, no-store")
        return r.body

    def test_executive(self):
        c = session(["executive"])
        body = self.check_executive(c)
        self.assertIn("Notion", body)
        self.assertNotIn("運用ダッシュボード", body)
        r = c.req("/admin/")
        self.assertEqual(r.status, 403)
        self.assertIn("管理者", r.body)
        self.assertNotIn("COLLECTOR-OK", r.body)
        self.assertEqual(r.headers["Cache-Control"], "private, no-store")

    def test_executive_admin(self):
        c = session(["executive", "admin"])
        body = self.check_executive(c)
        self.assertIn("運用ダッシュボード", body)
        r = c.req("/admin/")
        self.assertEqual((r.status, r.body), (200, "COLLECTOR-OK"))
        self.assertEqual(r.headers["Cache-Control"], "private, no-store")

    def test_no_role(self):
        c = session(None)
        r = c.req("/")
        self.assertEqual(r.status, 403)
        self.assertNotIn("HOMER-STUB", r.body)
        r = c.req("/assets/config.yml")
        self.assertEqual(r.status, 403)
        self.assertNotIn("Notion", r.body)
        self.assertEqual(c.req("/admin/").status, 403)

    def test_lookalike_group(self):
        # executive を持つが admin ではない。部分一致する別名を admin と扱わない
        for g in ("notadmin", "admin2", "administrators"):
            c = session(["executive", g])
            self.assertNotIn("運用ダッシュボード", self.check_executive(c), g)
            self.assertEqual(c.req("/admin/").status, 403, g)
        # executive の別名だけでは入れない
        c = session(["notexecutive"])
        self.assertEqual(c.req("/").status, 403)

    def test_unauthenticated(self):
        c = Client()
        r = c.req("/")
        self.assertEqual(r.status, 302)
        self.assertTrue(r.headers["Location"].startswith("/oauth2/start"), r.headers["Location"])
        r = c.req("/admin/")
        self.assertEqual(r.status, 302)
        self.assertTrue(r.headers["Location"].startswith("/oauth2/start"), r.headers["Location"])
        self.assertTrue(c.req("/admin").headers["Location"].startswith("/admin/"))
        # config は転送ではなく 401 (Homer が転送を設定なしと扱うため)
        r = c.req("/assets/config.yml")
        self.assertEqual(r.status, 401)
        self.assertNotIn("Notion", r.body)
        self.assertEqual(r.headers["Cache-Control"], "private, no-store")

    def test_login_redirect_by_sec_fetch_mode(self):
        # fetch/XHR は転送先の IdP を CORS で追えないため 401、ページ遷移だけ 302
        for mode, want in (("navigate", 302), (None, 302), ("no-cors", 401), ("cors", 401), ("same-origin", 401)):
            r = Client().req("/", headers={"Sec-Fetch-Mode": mode} if mode else None)
            self.assertEqual(r.status, want, mode)
            if want == 302:
                self.assertTrue(r.headers["Location"].startswith("/oauth2/start"), mode)

    def test_concurrent_login_flows_share_cookie_jar(self):
        # 同一ブラウザで start を 2 回続けても、先の flow の callback が PKCE verifier を失わず通る。
        # CSRF cookie が flow ごとに別名で共存することが前提
        c = Client()
        auth_a = c.start()
        auth_b = c.start()
        csrf = sorted(k.name for k in c.jar if "csrf" in k.name)
        self.assertEqual(len(csrf), 2, csrf)
        self.assertEqual(len(set(csrf)), 2)
        r = c.finish(auth_a, ["executive"])
        self.assertEqual(r.headers["Location"], "/")
        self.assertEqual(c.req("/").status, 200)
        c.finish(auth_b, ["executive"])
        self.assertEqual(c.req("/").status, 200)

    def test_login_redirect_has_no_header_injection(self):
        r = Client().req("/a%0d%0aSet-Cookie:%20pwn=1")
        self.assertEqual(r.status, 302)
        self.assertIsNone(r.headers["Set-Cookie"])
        self.assertNotIn("\n", r.headers["Location"])

    def test_sign_out_ends_idp_session(self):
        # ブラウザを Zitadel の end_session へ送り、ID token を id_token_hint に載せる
        c = session(["executive"])
        r = c.req("/oauth2/sign_out")
        self.assertEqual(r.status, 302)
        loc = urllib.parse.urlsplit(r.headers["Location"])
        self.assertEqual((loc.netloc, loc.path), ("idp.aramakisai.com", "/oidc/v1/end_session"))
        q = urllib.parse.parse_qs(loc.query)
        self.assertEqual(q["post_logout_redirect_uri"], ["https://dash.aramakisai.com/"])
        self.assertEqual(q["id_token_hint"][0].count("."), 2)  # JWT に置換済み
        self.assertEqual(c.req("/").status, 302)

    def test_sign_out_without_session_skips_idp(self):
        # セッションが無い (未ログイン・壊れた Cookie) と {id_token} が置換されず end_session が 400 になるため、
        # IdP を経由せずポータルへ戻す
        for cookie in (None, "_oauth2_proxy=invalid"):
            r = Client().req("/oauth2/sign_out", headers={"Cookie": cookie} if cookie else None)
            self.assertEqual(r.status, 302, cookie)
            self.assertEqual(r.headers["Location"], "/", cookie)

    def test_static_routes(self):
        for c in (Client(), session(["executive", "admin"])):
            for p in ("/assets/config-admin.yml", "/assets/links.yaml", "/assets/admin-overlay.yaml", "/assets/config.yml.dist"):
                self.assertEqual(c.req(p).status, 404, p)
            r = c.req("/denied/denied.html")
            self.assertEqual(r.status, 200)
            self.assertNotIn("Notion", r.body)
            self.assertEqual(c.req("/oauth2/sign_out").status in (200, 302), True)

    def test_static_assets_skip_auth(self):
        # サブリソースが auth_request を踏むと並行 refresh の原因になるため、認証なしで返す
        for p in ("/resources/font.woff2", "/assets/icons/icon.png", "/assets/themes/theme.css", "/logo.png", "/assets/manifest.json"):
            r = Client().req(p)
            self.assertEqual((r.status, r.headers.get("Set-Cookie")), (200, None), p)
        self.assertEqual(Client().req("/resources/missing.js").status, 404)

    def test_protected_stay_protected(self):
        # Homer 本体・設定・Service Worker は従来どおり認証が要る。設定系の 404 も静的 prefix に負けない
        c = Client()
        for p in ("/", "/index.html", "/sw.js", "/registerSW.js"):
            self.assertEqual(c.req(p, headers={"Sec-Fetch-Mode": "navigate"}).status, 302, p)
        self.assertEqual(c.req("/assets/config.yml").status, 401)
        for p in ("/assets/config-admin.yml", "/assets/links.yaml", "/assets/admin-overlay.yaml"):
            self.assertEqual(c.req(p).status, 404, p)

    def test_refresh_cookie_returned(self):
        # redis ストアでも cookie-refresh 後のセッション cookie がブラウザへ届く
        c = session(["executive"])
        self.assertEqual(c.req("/").status, 200)
        self.assertEqual(len([k for k in c.jar if k.name == "_oauth2_proxy"]), 1)


if __name__ == "__main__":
    unittest.main()
