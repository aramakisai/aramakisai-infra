#!/usr/bin/env python3
"""verify-k3s-authn-disposable.sh の補助。OIDC 発行者のモック、自前署名トークンの判定、監査ログの総量負荷を担う。

serve  : discovery と JWKS を返す HTTPS サーバ (--cacerts 指定時は /cacerts も返す)
tokensrv: GitHub の ID トークン要求エンドポイントのモック (HTTP)。要求トークン一致時に audience 指定のトークンを返す
patch  : 描画済み認証設定の issuer URL と CA だけをテスト用に差し替える (照合規則は触らない)
judge  : 許可・違反の全ケースを API サーバに提示して判定する
probe  : 有効なトークンが 401 になることを確認する (発行者に到達できない状態用)
flood  : 有効なトークンで大量に要求を送り、監査ログを増やす
"""
import argparse, http.client, json, ssl, sys, threading, time
from urllib.parse import parse_qs, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

import jwt
import yaml
from cryptography.hazmat.primitives import serialization
from jwt.algorithms import RSAAlgorithm

KID = "verify-k1"


def load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def serve(a):
    jwk = json.loads(RSAAlgorithm.to_jwk(load_key(a.key).public_key()))
    jwk.update(kid=KID, use="sig", alg="RS256")
    docs = {
        "/.well-known/openid-configuration": {"issuer": a.issuer, "jwks_uri": a.issuer + "/jwks",
                                              "id_token_signing_alg_values_supported": ["RS256"]},
        "/jwks": {"keys": [jwk]},
    }
    cacerts = open(a.cacerts, "rb").read() if a.cacerts else None

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            d = docs.get(self.path)
            body = json.dumps(d or {}).encode()
            if cacerts is not None and self.path == "/cacerts":
                d, body = True, cacerts
            self.send_response(200 if d else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(a.cert, a.tls_key)
    srv = HTTPServer((a.bind, a.port), H)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()


def tokensrv(a):
    x = Ctx(a)

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if self.headers.get("Authorization") != f"bearer {a.request_token}" or q.get("api-version") != ["2.0"]:
                self.send_response(401)
                self.end_headers()
                return
            aud = (q.get("audience") or [None])[0]
            body = json.dumps({"value": x.sign(dict(x.base(), exp=int(time.time()) + 300), aud=aud)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    HTTPServer(("127.0.0.1", a.port), H).serve_forever()


def patch(a):
    cfg = yaml.safe_load(open(a.inp))
    cfg["jwt"][0]["issuer"]["url"] = a.issuer
    cfg["jwt"][0]["issuer"]["certificateAuthority"] = open(a.ca).read()
    yaml.safe_dump(cfg, open(a.out, "w"), width=10**9, default_flow_style=False)


class Ctx:
    def __init__(self, a):
        self.cfg = yaml.safe_load(open(a.defaults))
        self.iss = a.issuer
        self.key = load_key(a.key)
        self.other_key = None
        self.host, self.port = a.api.split(":")
        self.tls = ssl._create_unverified_context()

    def sign(self, claims, aud=None, iss=None, key=None):
        now = int(time.time())
        c = {"iss": iss or self.iss, "aud": aud or self.cfg["k3s_github_oidc_audience"],
             "iat": now, "nbf": now - 5, "exp": now + 3600, "sub": "repo:x:y"}
        c.update(claims)
        c = {k: v for k, v in c.items() if v is not None}
        return jwt.encode(c, key or self.key, algorithm="RS256", headers={"kid": KID})

    def base(self, wf="infra-health-check.yml", event="workflow_dispatch", env=None):
        repo = self.cfg["k3s_github_oidc_repository"]
        c = {"repository_owner_id": str(self.cfg["k3s_github_oidc_repository_owner_id"]),
             "repository_id": str(self.cfg["k3s_github_oidc_repository_id"]),
             "repository": repo, "ref": "refs/heads/main", "event_name": event,
             "job_workflow_ref": f"{repo}/.github/workflows/{wf}@refs/heads/main",
             "runner_environment": "github-hosted"}
        if env:
            c["environment"] = env
        return c

    def whoami(self, token):
        conn = http.client.HTTPSConnection(self.host, int(self.port), context=self.tls, timeout=30)
        return self._whoami(conn, token)

    def _whoami(self, conn, token):
        conn.request("POST", "/apis/authentication.k8s.io/v1/selfsubjectreviews",
                     body=json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"}),
                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        r = conn.getresponse()
        body = r.read()
        user = None
        if r.status in (200, 201):
            user = json.loads(body)["status"]["userInfo"]["username"]
        return r.status, user


def cases(x):
    repo = x.cfg["k3s_github_oidc_repository"]
    wfs = x.cfg["k3s_github_oidc_workflows"]
    allow, deny = [], []
    for wf, p in wfs.items():
        for ev in p["events"]:
            allow.append((f"許可 {wf} {ev}", x.sign(x.base(wf, ev, p.get("environment"))), "gha:" + wf.removesuffix(".yml")))
    allow.append(("許可 environment 不要のワークフローに environment claim が付いても通る",
                  x.sign(x.base(env="production")), "gha:infra-health-check"))

    def d(name, claims=None, **kw):
        deny.append((name, x.sign(claims if claims is not None else x.base(), **kw), None))

    def mut(**kw):
        c = x.base()
        c.update(kw)
        return {k: v for k, v in c.items() if v is not None}

    d("拒否 他組織 ID", mut(repository_owner_id="1"))
    d("拒否 他リポジトリ ID", mut(repository_id="1"))
    d("拒否 フォーク", mut(repository_id="1", repository="forker/aramakisai-infra",
                          job_workflow_ref="forker/aramakisai-infra/.github/workflows/infra-health-check.yml@refs/heads/main"))
    d("拒否 ref が feature ブランチ", mut(ref="refs/heads/feat/x"))
    d("拒否 ref がタグ", mut(ref="refs/tags/v1"))
    d("拒否 ref 欠落", mut(ref=None))
    d("拒否 許可リスト外ワークフロー", mut(job_workflow_ref=f"{repo}/.github/workflows/k3s-upgrade.yml@refs/heads/main"))
    d("拒否 外部リポジトリの同名 reusable workflow",
      mut(job_workflow_ref="other-org/shared/.github/workflows/infra-health-check.yml@refs/heads/main"))
    d("拒否 job_workflow_ref が feature ブランチ", mut(job_workflow_ref=f"{repo}/.github/workflows/infra-health-check.yml@refs/heads/feat"))
    d("拒否 サブディレクトリ", mut(job_workflow_ref=f"{repo}/.github/workflows/sub/infra-health-check.yml@refs/heads/main"))
    for ev in ("pull_request", "pull_request_target", "push"):
        d(f"拒否 event {ev}", mut(event_name=ev))
    d("拒否 許可外イベントの組み合わせ (intrusion-response + schedule)",
      x.base("intrusion-response.yml", "schedule"))
    d("拒否 self-hosted ランナー", mut(runner_environment="self-hosted"))
    d("拒否 runner_environment 欠落", mut(runner_environment=None))
    d("拒否 dr-recovery environment なし", x.base("dr-recovery.yml"))
    d("拒否 dr-recovery environment 不一致", x.base("dr-recovery.yml", env="production"))
    d("拒否 dr-recovery environment 空", x.base("dr-recovery.yml", env=""))
    deny.append(("拒否 audience のみ一致 (他の claim なし)", x.sign({}), None))
    for k in ("repository_owner_id", "repository_id", "event_name", "job_workflow_ref"):
        d(f"拒否 claim 欠落 {k}", mut(**{k: None}))
    d("拒否 audience 不一致", aud="other-audience")
    d("拒否 期限切れ", mut(exp=int(time.time()) - 60, iat=int(time.time()) - 400, nbf=int(time.time()) - 400))
    d("拒否 nbf が未来", mut(nbf=int(time.time()) + 600))
    d("拒否 issuer 不一致", iss="https://other.example")
    from cryptography.hazmat.primitives.asymmetric import rsa
    d("拒否 署名鍵が違う", key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return allow + deny


def judge(a):
    x = Ctx(a)
    bad = 0
    cs = cases(x)
    for name, tok, want in cs:
        st, user = x.whoami(tok)
        ok = (st == 200 or st == 201) and user == want if want else st == 401
        bad += not ok
        print(f"  {'PASS' if ok else 'FAIL'} {name} -> {st}{' ' + user if user else ''}")
    print(f"judge: {len(cs) - bad}/{len(cs)}")
    sys.exit(1 if bad else 0)


def probe(a):
    x = Ctx(a)
    st, _ = x.whoami(x.sign(x.base()))
    print(f"probe: status {st}")
    sys.exit(0 if st == 401 else 1)


def flood(a):
    x = Ctx(a)
    tok = x.sign(x.base())
    per = a.count // 8

    def run():
        conn = http.client.HTTPSConnection(x.host, int(x.port), context=x.tls, timeout=30)
        for _ in range(per):
            x._whoami(conn, tok)

    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]


def main():
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("serve")
    s.add_argument("--issuer", required=True)
    s.add_argument("--bind", required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--cert", required=True)
    s.add_argument("--tls-key", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--cacerts")
    s.set_defaults(f=serve)
    s = sp.add_parser("tokensrv")
    s.add_argument("--defaults", required=True)
    s.add_argument("--issuer", required=True)
    s.add_argument("--key", required=True)
    s.add_argument("--port", type=int, required=True)
    s.add_argument("--request-token", required=True)
    s.add_argument("--api", default="0:0")
    s.set_defaults(f=tokensrv)
    s = sp.add_parser("patch")
    s.add_argument("--inp", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--issuer", required=True)
    s.add_argument("--ca", required=True)
    s.set_defaults(f=patch)
    for n, f in (("judge", judge), ("probe", probe), ("flood", flood)):
        s = sp.add_parser(n)
        s.add_argument("--defaults", required=True)
        s.add_argument("--issuer", required=True)
        s.add_argument("--key", required=True)
        s.add_argument("--api", required=True, help="host:port")
        if n == "flood":
            s.add_argument("--count", type=int, required=True)
        s.set_defaults(f=f)
    a = p.parse_args()
    a.f(a)


main()
