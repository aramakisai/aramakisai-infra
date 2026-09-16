#!/usr/bin/env python3
"""カットオーバー各ステップの動作検証(task9.4)。

サブコマンド:
  oidc-e2e: Session API -> /oauth/v2/authorize -> CreateCallback -> token交換
            -> userinfo までEnd-to-Endで成功することを確認する
            (design.md「OIDCログインフロー(RPアプリ共通)」)。RPアプリ実体を
            デプロイせずとも、同一Projectに登録されたOIDC Clientが正しく
            動作することを確認できる(task5.1がCMSで採用した代替手法と同型)。
"""
import argparse
import base64
import json
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirect(HTTPRedirectHandler):
    # /oauth/v2/authorizeは302でlogin v2 UI(クラスタ内DNS、ansible実行ホストからは
    # 到達不能)へリダイレクトする。Locationヘッダからauth_request_idだけを読み
    # 取りたいため、既定のリダイレクト追従を無効化する。
    def redirect_request(self, *args, **kwargs):
        return None


_opener = build_opener(_NoRedirect)


def log_event(event: str, **kwargs):
    print(json.dumps({"event": event, **kwargs}, ensure_ascii=False))


def http_call(url: str, method: str = "GET", headers: dict | None = None, body: bytes | None = None):
    req = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with _opener.open(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def oidc_e2e(args) -> int:
    base = args.base_url.rstrip("/")

    # 1. Session API でパスワード認証する (login-client PATで呼ぶ、design.md
    # 「招待オンボーディングフロー」と同じ経路)
    status, _, body = http_call(
        f"{base}/v2/sessions",
        method="POST",
        headers={
            "Authorization": f"Bearer {args.login_pat}",
            "Content-Type": "application/json",
            "Host": args.external_domain,
        },
        body=json.dumps(
            {"checks": {"user": {"loginName": args.username}, "password": {"password": args.password}}}
        ).encode(),
    )
    session = json.loads(body)
    if status != 201:
        log_event("session_failed", status=status, response=session)
        return 1
    session_id, session_token = session["sessionId"], session["sessionToken"]
    log_event("session_ok", session_id=session_id)

    # 2. /oauth/v2/authorize で authRequestId を得る (302 Locationから抽出)
    query = urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": args.redirect_uri,
            "response_type": "code",
            "scope": "openid profile email",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
        }
    )
    status, headers, _ = http_call(f"{base}/oauth/v2/authorize?{query}", headers={"Host": args.external_domain})
    location = headers.get("Location", "")
    auth_request_id = parse_qs(urlparse(location).query).get("authRequest", [None])[0]
    if not auth_request_id:
        log_event("authorize_failed", status=status, location=location)
        return 1
    log_event("authorize_ok", auth_request_id=auth_request_id)

    # 3. CreateCallback (login-client PAT)
    status, _, body = http_call(
        f"{base}/v2/oidc/auth_requests/{auth_request_id}",
        method="POST",
        headers={
            "Authorization": f"Bearer {args.login_pat}",
            "Content-Type": "application/json",
            "Host": args.external_domain,
        },
        body=json.dumps({"session": {"sessionId": session_id, "sessionToken": session_token}}).encode(),
    )
    callback = json.loads(body)
    if status != 200 or "callbackUrl" not in callback:
        log_event("callback_failed", status=status, response=callback)
        return 1
    code = parse_qs(urlparse(callback["callbackUrl"]).query).get("code", [None])[0]
    log_event("callback_ok")

    # 4. token交換 (RFC7636 既知テストベクタのcode_verifierを使用、code_challengeと対)
    basic = base64.b64encode(f"{args.client_id}:{args.client_secret}".encode()).decode()
    status, _, body = http_call(
        f"{base}/oauth/v2/token",
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": args.external_domain,
        },
        body=urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": args.redirect_uri,
                "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
            }
        ).encode(),
    )
    token = json.loads(body)
    if status != 200 or "access_token" not in token:
        log_event("token_failed", status=status, response=token)
        return 1
    log_event("token_ok")

    # 5. userinfo
    status, _, body = http_call(
        f"{base}/oidc/v1/userinfo",
        headers={"Authorization": f"Bearer {token['access_token']}", "Host": args.external_domain},
    )
    userinfo = json.loads(body)
    if status != 200 or userinfo.get("email") != args.username:
        log_event("userinfo_failed", status=status, response=userinfo)
        return 1
    log_event("userinfo_ok", email=userinfo.get("email"))

    log_event("oidc_e2e_pass")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Zitadel cutover step verification.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("oidc-e2e")
    p.add_argument("--base-url", required=True)
    p.add_argument("--external-domain", required=True)
    p.add_argument("--login-pat", required=True)
    p.add_argument("--client-id", required=True)
    p.add_argument("--client-secret", required=True)
    p.add_argument("--redirect-uri", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)

    args = parser.parse_args()
    if args.command == "oidc-e2e":
        sys.exit(oidc_e2e(args))


if __name__ == "__main__":
    main()
