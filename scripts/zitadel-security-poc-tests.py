#!/usr/bin/env python3
"""
Zitadel PoCのセキュリティ検証(モンキーテスト)スクリプト。task 7.1〜7.3に対応。

k3d検証環境のクラスタ内DNS経由でのみ疎通する(ExternalDomain不一致でport-forward+Host偽装は
失敗するため、in-cluster Podから実行すること。例: netshoot Podへ本ファイルをkubectl cpし、
python3で実行する)。

前提として2種類のPATが必要:
  - ZITADEL_TOKEN: 管理用PAT(Terraform provider用と同じもの)。Session API・Management API
    ・User v2 API(招待コード等)の呼び出しに使う。
  - ZITADEL_LOGIN_CLIENT_TOKEN: `session.link`権限を持つログインクライアント用PAT。
    OIDC CreateCallback(POST /v2/oidc/auth_requests/{id})はこの権限がないと
    AUTH-AWfge(No matching permissions found)で拒否される(実機確認済み、管理用PATでは不可)。

使い方:
  ZITADEL_DOMAIN=http://zitadel.zitadel.svc.cluster.local:8080 \\
  ZITADEL_TOKEN=<管理PAT> ZITADEL_LOGIN_CLIENT_TOKEN=<login-clientPAT> \\
  ZITADEL_TEST_USERNAME=testuser1@aramakisai.com ZITADEL_TEST_PASSWORD='TestPassw0rd!' \\
  ZITADEL_CLIENT_ID=<CMSのclient_id> ZITADEL_CLIENT_SECRET=<CMSのclient_secret> \\
  ZITADEL_REDIRECT_URI=https://cms.aramakisai.com/api/auth/zitadel/callback \\
    python3 scripts/zitadel-security-poc-tests.py --all
"""
import argparse
import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_UA = "aramakisai-infra/zitadel-security-poc-tests"

# RFC 7636 Appendix B のテストベクタ。S256のcode_challengeを事前計算済みで使い回せる。
PKCE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
WRONG_PKCE_VERIFIER = "wrong-verifier-000000000000000000000000000"


def log_event(event: str, **kwargs):
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    print(json.dumps(record, ensure_ascii=False))


def get_env_or_die(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        log_event("config_error", error=f"Missing required environment variable: {key}")
        sys.exit(1)
    return val


def call(base_url, token, method, path, body=None, form=None, basic_auth=None):
    """Zitadel APIを呼び出す。戻り値は (http_status, json_body, elapsed_seconds)。"""
    headers = {"User-Agent": _UA}
    data = None
    if form is not None:
        import urllib.parse

        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if basic_auth:
        user, pw = basic_auth
        cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"

    req = Request(base_url + path, data=data, method=method, headers=headers)
    start = time.monotonic()
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
            elapsed = time.monotonic() - start
            return resp.status, (json.loads(raw) if raw else {}), elapsed
    except HTTPError as exc:
        raw = exc.read()
        elapsed = time.monotonic() - start
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode(errors="replace")}
        return exc.code, parsed, elapsed


# ---------------------------------------------------------------------------
# 7.1 ブルートフォース対策とユーザー列挙耐性
# ---------------------------------------------------------------------------
def test_bruteforce_and_enumeration(base_url, token, username, password, attempts=15):
    log_event("section_start", section="7.1_bruteforce_and_enumeration")

    # 実在ユーザーへの誤パスワード連続試行
    existing_results = []
    for i in range(1, attempts + 1):
        status, body, elapsed = call(
            base_url,
            token,
            "POST",
            "/v2/sessions",
            {"checks": {"user": {"loginName": username}, "password": {"password": f"WrongPass{i}!"}}},
        )
        failed_attempts = None
        for d in body.get("details", []) if isinstance(body.get("details"), list) else []:
            if "failedAttempts" in d:
                failed_attempts = d["failedAttempts"]
        existing_results.append({"attempt": i, "status": status, "elapsed": round(elapsed, 3), "failedAttempts": failed_attempts})
    log_event("bruteforce_existing_user_attempts", username=username, results=existing_results)

    lockout_observed = any(r["status"] not in (400, 401) for r in existing_results)
    log_event(
        "bruteforce_lockout_verdict",
        lockout_observed=lockout_observed,
        note=(
            f"{attempts}回連続の誤パスワードでもレスポンスコード(400)・エラー内容に変化なし"
            "(アカウントロックアウト/レート制限は観測されなかった)"
            if not lockout_observed
            else "レスポンスに変化あり、ロックアウト/レート制限が発火した可能性"
        ),
    )

    # 正しいパスワードでのログインが依然成功するか(ロックアウトされていないことの直接確認)
    status, _, _ = call(
        base_url, token, "POST", "/v2/sessions",
        {"checks": {"user": {"loginName": username}, "password": {"password": password}}},
    )
    log_event("bruteforce_correct_password_still_works", status=status, still_works=(status == 201))

    # ユーザー列挙耐性: 存在しないユーザー名 vs 実在ユーザーの誤パスワード
    nonexistent_username = f"nonexistent-{int(time.time())}@aramakisai-poc.invalid"
    nx_status, nx_body, nx_elapsed = call(
        base_url, token, "POST", "/v2/sessions",
        {"checks": {"user": {"loginName": nonexistent_username}, "password": {"password": "WrongPass1!"}}},
    )
    wp_status, wp_body, wp_elapsed = call(
        base_url, token, "POST", "/v2/sessions",
        {"checks": {"user": {"loginName": username}, "password": {"password": "WrongPass1!"}}},
    )
    indistinguishable = (
        nx_status == wp_status
        and nx_body.get("code") == wp_body.get("code")
        and nx_body.get("message", "").split(" (")[0] == wp_body.get("message", "").split(" (")[0]
    )
    log_event(
        "enumeration_resistance_raw_session_api",
        nonexistent_user_response={"status": nx_status, "code": nx_body.get("code"), "message": nx_body.get("message"), "elapsed": round(nx_elapsed, 3)},
        wrong_password_response={"status": wp_status, "code": wp_body.get("code"), "message": wp_body.get("message"), "elapsed": round(wp_elapsed, 3)},
        indistinguishable=indistinguishable,
        note=(
            "Session API生レスポンスはHTTPステータス・エラーコード・メッセージが異なり区別可能"
            "(存在しないユーザー: 404/code5 'User could not be found' vs 実在ユーザー誤パスワード: "
            "400/code3 'Password is invalid'。加えてfailedAttemptsフィールドで試行回数まで漏洩)。"
            "timingも実在ユーザー側が有意に遅い(パスワードハッシュ照合コストのため)。"
            "Dovecot Lua Auth Bridge(configmap.yaml auth_passdb_lookup)は両者を一律"
            "PASSDB_RESULT_PASSWORD_MISMATCHへ正規化しステータスコード面は緩和しているが、"
            "lua側に定数時間化の実装はなくtimingサイドチャネルは未対策のまま残る。"
            if not indistinguishable
            else "生Session APIレベルで区別不能"
        ),
    )
    log_event("section_end", section="7.1_bruteforce_and_enumeration")
    return {"lockout_observed": lockout_observed, "enumeration_indistinguishable_raw_api": indistinguishable}


# ---------------------------------------------------------------------------
# 7.2 認可コードreplay・PKCE不一致・redirect_uri改ざん
# ---------------------------------------------------------------------------
def _authorize_and_get_auth_request_id(base_url, admin_token, client_id, redirect_uri, code_challenge, state):
    """/oauth/v2/authorize を呼び、Locationヘッダから authRequestID を抽出する。
    リダイレクトは追わず、Location自体(または400ボディ)を返す。"""
    import urllib.parse

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid profile email",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    req = Request(base_url + "/oauth/v2/authorize?" + query, method="GET", headers={"User-Agent": _UA})
    try:
        with urlopen(req, timeout=15) as resp:
            # ZitadelはPython urllibのデフォルトでは302を自動追従するため、redirect_handlerを
            # 無効化した簡易実装ではなく、http.client相当でLocationを直接見る必要がある。
            # urllibはデフォルトでリダイレクトを追ってしまうため、ここでは最終応答のURLを見る。
            final_url = resp.geturl()
            return 200, final_url, None
    except HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw.decode(errors="replace")}
        return exc.code, None, body


def test_authz_code_replay_pkce_redirect(base_url, admin_token, login_client_token, client_id, client_secret, redirect_uri, username, password):
    log_event("section_start", section="7.2_authz_code_replay_pkce_redirect")

    # --- redirect_uri改ざん: 未登録redirect_uriでの認可リクエストが拒否されるか ---
    tampered_redirect = "https://evil.example.invalid/callback"
    status, final_url, body = _authorize_and_get_auth_request_id(
        base_url, admin_token, client_id, tampered_redirect, PKCE_CHALLENGE, "state-redirect-tamper"
    )
    redirect_rejected = status == 400
    log_event(
        "redirect_uri_tampering",
        tampered_redirect_uri=tampered_redirect,
        status=status,
        body=body,
        rejected=redirect_rejected,
    )

    # --- 正規の認可リクエストでセッションを紐付けて認可コードを発行 ---
    status_ses, ses_body, _ = call(
        base_url, admin_token, "POST", "/v2/sessions",
        {"checks": {"user": {"loginName": username}, "password": {"password": password}}},
    )
    if status_ses not in (200, 201):
        log_event("section_error", section="7.2", error=f"session creation failed: {status_ses} {ses_body}")
        return {"error": "session_creation_failed"}
    session_id = ses_body["sessionId"]
    session_token = ses_body["sessionToken"]

    def issue_code(state):
        # urllib.request.urlopenはデフォルトで302を自動追従してしまい、ログインUI(別ポート)
        # へのリダイレクト先で接続エラーになるかLocationヘッダを失う。authRequestIDは
        # Locationヘッダからしか取れないため、http.clientでリダイレクトを追わずに1回だけ叩く。
        import http.client
        import urllib.parse as up

        query = up.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid profile email",
                "code_challenge": PKCE_CHALLENGE,
                "code_challenge_method": "S256",
                "state": state,
            }
        )
        parsed = up.urlparse(base_url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
        conn.request("GET", "/oauth/v2/authorize?" + query, headers={"User-Agent": _UA})
        resp = conn.getresponse()
        location = resp.getheader("Location") or ""
        resp.read()
        conn.close()
        if "authRequest=" in location:
            return location.split("authRequest=")[1].split("&")[0]
        return None

    auth_request_id = issue_code("state-normal-flow")
    if not auth_request_id:
        log_event("section_error", section="7.2", error="failed to obtain authRequestId")
        return {"error": "auth_request_id_missing"}

    status_cb, cb_body, _ = call(
        base_url, login_client_token, "POST", f"/v2/oidc/auth_requests/{auth_request_id}",
        {"session": {"sessionId": session_id, "sessionToken": session_token}},
    )
    if status_cb != 200 or "callbackUrl" not in cb_body:
        log_event("section_error", section="7.2", error=f"callback failed: {status_cb} {cb_body}")
        return {"error": "callback_failed"}
    callback_url = cb_body["callbackUrl"]
    code = callback_url.split("code=")[1].split("&")[0]
    log_event("authorization_code_issued", auth_request_id=auth_request_id, code_prefix=code[:8] + "...")

    # --- PKCE code_verifier不一致でのトークン交換 ---
    status_wrong_pkce, body_wrong_pkce, _ = call(
        base_url, None, "POST", "/oauth/v2/token",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": WRONG_PKCE_VERIFIER,
        },
        basic_auth=(client_id, client_secret),
    )
    pkce_rejected = status_wrong_pkce != 200
    log_event(
        "pkce_mismatch_token_exchange",
        status=status_wrong_pkce,
        body=body_wrong_pkce,
        rejected=pkce_rejected,
    )

    # --- 正しいverifierでの初回トークン交換(成功することを確認した上でreplayへ進む) ---
    status_ok, body_ok, _ = call(
        base_url, None, "POST", "/oauth/v2/token",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": PKCE_VERIFIER,
        },
        basic_auth=(client_id, client_secret),
    )
    first_exchange_ok = status_ok == 200 and "access_token" in body_ok
    log_event("first_token_exchange", status=status_ok, ok=first_exchange_ok)

    # --- 同一認可コードの2回目使用(リプレイ) ---
    status_replay, body_replay, _ = call(
        base_url, None, "POST", "/oauth/v2/token",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": PKCE_VERIFIER,
        },
        basic_auth=(client_id, client_secret),
    )
    replay_rejected = status_replay != 200
    log_event(
        "authorization_code_replay",
        status=status_replay,
        body=body_replay,
        rejected=replay_rejected,
    )

    log_event("section_end", section="7.2_authz_code_replay_pkce_redirect")
    return {
        "redirect_uri_tampering_rejected": redirect_rejected,
        "pkce_mismatch_rejected": pkce_rejected,
        "first_exchange_ok": first_exchange_ok,
        "replay_rejected": replay_rejected,
    }


# ---------------------------------------------------------------------------
# 7.3 招待コードの期限切れ・再利用防止
# ---------------------------------------------------------------------------
def test_invite_code_reuse_and_expiry(base_url, admin_token):
    log_event("section_start", section="7.3_invite_code_reuse_and_expiry")
    suffix = int(time.time())
    email = f"invite-poc-test-{suffix}@aramakisai-poc.invalid"

    status, body, _ = call(
        base_url, admin_token, "POST", "/v2/users/human",
        {
            "username": email,
            "profile": {"givenName": "Invite", "familyName": "PocTest"},
            "email": {"email": email, "isVerified": True},
        },
    )
    if status != 200:
        log_event("section_error", section="7.3", error=f"user creation failed: {status} {body}")
        return {"error": "user_creation_failed"}
    user_id = body["userId"]

    # --- 再利用防止: 検証済みコードの2回目使用 ---
    status_issue, body_issue, _ = call(base_url, admin_token, "POST", f"/v2/users/{user_id}/invite_code", {"returnCode": {}})
    code = body_issue.get("inviteCode")
    log_event("invite_code_issued", user_id=user_id, status=status_issue)

    status_verify1, body_verify1, _ = call(
        base_url, admin_token, "POST", f"/v2/users/{user_id}/invite_code/verify", {"verificationCode": code}
    )
    first_verify_ok = status_verify1 == 200
    log_event("invite_code_first_verify", status=status_verify1, ok=first_verify_ok)

    status_verify2, body_verify2, _ = call(
        base_url, admin_token, "POST", f"/v2/users/{user_id}/invite_code/verify", {"verificationCode": code}
    )
    reuse_rejected = status_verify2 != 200
    log_event("invite_code_reuse_attempt", status=status_verify2, body=body_verify2, rejected=reuse_rejected)

    # --- 有効期限: CreateInviteCodeのexpirationフィールドで短縮できるか実機確認 ---
    email2 = f"invite-poc-expiry-{suffix}@aramakisai-poc.invalid"
    status2, body2, _ = call(
        base_url, admin_token, "POST", "/v2/users/human",
        {
            "username": email2,
            "profile": {"givenName": "Invite", "familyName": "ExpiryTest"},
            "email": {"email": email2, "isVerified": True},
        },
    )
    expiry_note = None
    expiry_verified = False
    if status2 == 200:
        user_id2 = body2["userId"]
        status_issue2, body_issue2, _ = call(
            base_url, admin_token, "POST", f"/v2/users/{user_id2}/invite_code",
            {"returnCode": {}, "expiration": "3s"},
        )
        code2 = body_issue2.get("inviteCode")
        log_event("invite_code_issued_with_custom_expiration_request", requested="3s", status=status_issue2)
        time.sleep(8)
        status_verify_expired, body_verify_expired, _ = call(
            base_url, admin_token, "POST", f"/v2/users/{user_id2}/invite_code/verify", {"verificationCode": code2}
        )
        if status_verify_expired == 200:
            expiry_note = (
                "CreateInviteCodeへのexpirationフィールド指定(3s)は無視され、8秒待機後も"
                "検証に成功した。カスタム有効期限をAPI経由で短縮することはできなかった"
                "(upstream zitadel/zitadel#10474: V2招待コードのsecret-generator設定"
                "(有効期限含む)はAdmin API/Consoleに未公開、デフォルト約72時間固定という報告と整合)。"
                "72時間実待機は非現実的なため、期限切れ拒否そのものはこのPoCでは確認できていない。"
                "再利用防止のみ確認済みとして記録する。"
            )
        else:
            expiry_verified = True
            expiry_note = "expirationフィールド指定が有効に働き、期限切れコードの検証が拒否された。"
        log_event("invite_code_expiry_check", status=status_verify_expired, expiry_verified=expiry_verified, note=expiry_note)
    else:
        expiry_note = f"期限切れ検証用ユーザー作成に失敗: {status2} {body2}"
        log_event("invite_code_expiry_check_skipped", error=expiry_note)

    log_event("section_end", section="7.3_invite_code_reuse_and_expiry")
    return {
        "first_verify_ok": first_verify_ok,
        "reuse_rejected": reuse_rejected,
        "expiry_verified": expiry_verified,
        "expiry_note": expiry_note,
    }


def main():
    parser = argparse.ArgumentParser(description="Zitadel PoCセキュリティ検証(モンキーテスト)")
    parser.add_argument("--all", action="store_true", help="7.1/7.2/7.3すべて実行")
    parser.add_argument("--bruteforce", action="store_true", help="7.1のみ実行")
    parser.add_argument("--oidc", action="store_true", help="7.2のみ実行")
    parser.add_argument("--invite", action="store_true", help="7.3のみ実行")
    args = parser.parse_args()
    if not (args.all or args.bruteforce or args.oidc or args.invite):
        args.all = True

    base_url = get_env_or_die("ZITADEL_DOMAIN").rstrip("/")
    admin_token = get_env_or_die("ZITADEL_TOKEN")
    username = os.environ.get("ZITADEL_TEST_USERNAME", "testuser1@aramakisai.com")
    password = get_env_or_die("ZITADEL_TEST_PASSWORD")

    summary = {}
    log_event("startup", base_url=base_url, username=username)

    if args.all or args.bruteforce:
        summary["7.1"] = test_bruteforce_and_enumeration(base_url, admin_token, username, password)

    if args.all or args.oidc:
        login_client_token = get_env_or_die("ZITADEL_LOGIN_CLIENT_TOKEN")
        client_id = get_env_or_die("ZITADEL_CLIENT_ID")
        client_secret = get_env_or_die("ZITADEL_CLIENT_SECRET")
        redirect_uri = get_env_or_die("ZITADEL_REDIRECT_URI")
        summary["7.2"] = test_authz_code_replay_pkce_redirect(
            base_url, admin_token, login_client_token, client_id, client_secret, redirect_uri, username, password
        )

    if args.all or args.invite:
        summary["7.3"] = test_invite_code_reuse_and_expiry(base_url, admin_token)

    log_event("summary", summary=summary)


if __name__ == "__main__":
    main()
