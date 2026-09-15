#!/usr/bin/env python3
"""
既存authentikユーザーをZitadelユーザーとして作成し、招待コードを発行するスクリプト。

Zitadel招待フロー(design.md「招待オンボーディングフロー」)では、パスワードは
Zitadel内で完結して設定される(AddHumanUserはpasswordを指定しない)。他コンポーネントへの
パスワード複製は発生しない。

使い方:
  # PoCサンプルユーザー(このファイルのSAMPLE_USERS)で招待を発行
  ZITADEL_DOMAIN=http://zitadel.zitadel.svc.cluster.local:8080 \\
  ZITADEL_PROJECT_ID=<project_id> ZITADEL_TOKEN=<PAT> \\
    python3 scripts/zitadel-invite-migration.py --dry-run

  # 実行(招待コードをAPIレスポンスで受け取る。SMTP未設定のk3d検証向け)
  ... python3 scripts/zitadel-invite-migration.py

  # 本番相当(SMTP設定済み)ではメール送信に切り替える
  ... python3 scripts/zitadel-invite-migration.py --send-email

  # 実際のauthentikユーザーを移行する場合は --input で本形式のJSONファイルを渡す:
  #   [{"email": "...", "given_name": "...", "family_name": "...", "groups": ["企画", "リーダー"]}, ...]
  # (email/groupsは既存authentikのUser一覧・グループ所属から作成する。本番prod-node-1の
  #  authentikには本スクリプトから直接アクセスしない — 別途エクスポートしたファイルを渡すこと)
  ... python3 scripts/zitadel-invite-migration.py --input users.json

ロールマッピング(authentikグループ表示名 → Zitadel project role_key)は
terraform/zitadel_projects.tf の aramakisai_project_roles と対応させている。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# terraform/zitadel_projects.tf の aramakisai_project_roles と同期させること
GROUP_DISPLAY_TO_ROLE_KEY = {
    "管理者": "admin",
    "実行委員": "executive",
    "リーダー": "leader",
    "企画": "planning",
    "会計": "accounting",
    "出店": "vendors",
    "出演": "performers",
    "広報": "pr",
    "総務": "general_affairs",
}

# PoC検証用ダミーユーザー(実authentikユーザーではない)。3〜5人・複数グループ所属を含む。
SAMPLE_USERS = [
    {
        "email": "sato.taro@aramakisai-poc.invalid",  # confidential:allow
        "given_name": "太郎",
        "family_name": "佐藤",
        "groups": ["企画", "リーダー"],
    },
    {
        "email": "suzuki.hanako@aramakisai-poc.invalid",  # confidential:allow
        "given_name": "花子",
        "family_name": "鈴木",
        "groups": ["会計"],
    },
    {
        "email": "tanaka.ichiro@aramakisai-poc.invalid",  # confidential:allow
        "given_name": "一郎",
        "family_name": "田中",
        "groups": ["出店", "出演"],
    },
    {
        "email": "yamamoto.megumi@aramakisai-poc.invalid",  # confidential:allow
        "given_name": "恵",
        "family_name": "山本",
        "groups": ["広報", "総務"],
    },
    {
        "email": "takahashi.osamu@aramakisai-poc.invalid",  # confidential:allow
        "given_name": "修",
        "family_name": "高橋",
        "groups": ["管理者", "実行委員"],
    },
]

_UA = "aramakisai-infra/zitadel-invite-migration"


def log_event(event: str, **kwargs):
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    print(json.dumps(record, ensure_ascii=False))


def get_env_or_die(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        log_event("config_error", error=f"Missing required environment variable: {key}")
        sys.exit(1)
    return val


def call(base_url: str, token: str, method: str, path: str, body: dict | None = None):
    """Zitadel APIを呼び出す。戻り値は (http_status, json_body)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        },
    )
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else {"raw": raw.decode(errors="replace")})


def find_user_id_by_username(base_url: str, token: str, username: str) -> str | None:
    status, body = call(
        base_url,
        token,
        "POST",
        "/v2/users",
        {"queries": [{"userNameQuery": {"userName": username}}]},
    )
    if status != 200:
        return None
    for user in body.get("result", []):
        if user.get("username") == username:
            return user["userId"]
    return None


def create_user(base_url: str, token: str, user: dict) -> tuple[str, bool]:
    """ユーザーを作成する。戻り値は (userId, created_new)。

    Zitadelは重複ユーザー名に対しHTTP 409(code=6 ALREADY_EXISTS)を返す(実機確認済み)。
    再実行時はこれを検知し既存ユーザーIDへフォールバックすることで冪等に動作させる。
    """
    email = user["email"]
    status, body = call(
        base_url,
        token,
        "POST",
        "/v2/users/human",
        {
            "username": email,
            "profile": {"givenName": user["given_name"], "familyName": user["family_name"]},
            # 招待コード検証(VerifyInviteCode)を初回認証手段の起点とするため、
            # ここではメール検証は既知の社内メールとしてisVerified=trueで作成する。
            "email": {"email": email, "isVerified": True},
        },
    )
    if status == 200:
        return body["userId"], True
    if status == 409:
        existing_id = find_user_id_by_username(base_url, token, email)
        if existing_id:
            return existing_id, False
    raise RuntimeError(f"create_user failed for {email}: HTTP {status} {body}")


def grant_roles(base_url: str, token: str, project_id: str, user_id: str, role_keys: list[str]):
    if not role_keys:
        return "no_roles"
    status, body = call(
        base_url,
        token,
        "POST",
        f"/management/v1/users/{user_id}/grants",
        {"projectId": project_id, "roleKeys": role_keys},
    )
    if status == 200:
        return "granted"
    if status == 409:
        return "already_granted"
    raise RuntimeError(f"grant_roles failed for user {user_id}: HTTP {status} {body}")


def create_invite_code(base_url: str, token: str, user_id: str, send_email: bool) -> str | None:
    # sendCode: 本番相当(SMTP設定済み)ではメール送信に委ねる。returnCode: SMTP未設定の
    # k3d検証環境ではAPIレスポンスで直接コードを受け取る(実機確認済み、design.md記載の
    # 「実メール送信基盤が無い場合はAPI経由で取得」に対応)。
    body = {"sendCode": {}} if send_email else {"returnCode": {}}
    status, resp = call(base_url, token, "POST", f"/v2/users/{user_id}/invite_code", body)
    if status != 200:
        raise RuntimeError(f"create_invite_code failed for user {user_id}: HTTP {status} {resp}")
    return resp.get("inviteCode")


def load_users(input_path: str | None) -> list[dict]:
    if not input_path:
        return SAMPLE_USERS
    with open(input_path, encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="既存authentikユーザーをZitadelへ作成し招待コードを発行する。"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="APIを呼ばず、実行予定の操作のみを表示する。"
    )
    parser.add_argument(
        "--input", type=str, help="移行対象ユーザーのJSONファイル(省略時はSAMPLE_USERS)。"
    )
    parser.add_argument(
        "--send-email",
        action="store_true",
        help="招待コードをAPIレスポンスで返さず、Zitadelのメール送信に委ねる(SMTP設定済み環境向け)。",
    )
    args = parser.parse_args()

    # ZITADEL_ORG_ID: 呼び出すManagement/v2 APIはいずれもPAT発行元の単一org(aramakisai)
    # コンテキストで動作し、org_idをリクエストへ含める必要はない(実機確認済み)ため未使用。
    base_url = project_id = token = ""
    if not args.dry_run:
        base_url = get_env_or_die("ZITADEL_DOMAIN").rstrip("/")
        project_id = get_env_or_die("ZITADEL_PROJECT_ID")
        token = get_env_or_die("ZITADEL_TOKEN")

    users = load_users(args.input)
    log_event("startup", mode="dry-run" if args.dry_run else "execute", user_count=len(users))

    succeeded = 0
    failures = []

    for user in users:
        email = user["email"]
        role_keys = []
        for group in user.get("groups", []):
            role_key = GROUP_DISPLAY_TO_ROLE_KEY.get(group)
            if role_key is None:
                log_event("unknown_group_skipped", email=email, group=group)
                continue
            role_keys.append(role_key)

        if args.dry_run:
            log_event(
                "dry_run_target",
                email=email,
                given_name=user["given_name"],
                family_name=user["family_name"],
                role_keys=role_keys,
            )
            succeeded += 1
            continue

        try:
            user_id, created = create_user(base_url, token, user)
            log_event("user_ready", email=email, user_id=user_id, created=created)

            grant_result = grant_roles(base_url, token, project_id, user_id, role_keys)
            log_event("grant_result", email=email, role_keys=role_keys, result=grant_result)

            invite_code = create_invite_code(base_url, token, user_id, args.send_email)
            if args.send_email:
                log_event("invite_email_sent", email=email, user_id=user_id)
            else:
                # returnCodeで取得したコードはSMTPの代替(k3d検証専用)。本番でメール配信
                # 不能な障害時の一時的な手動連絡以外でログに残さないこと。
                log_event("invite_code_issued", email=email, user_id=user_id, invite_code=invite_code)

            succeeded += 1
        except (RuntimeError, HTTPError, URLError, TimeoutError) as exc:
            log_event("user_migration_failed", email=email, error=str(exc))
            failures.append({"email": email, "error": str(exc)})

    log_event("summary", succeeded=succeeded, failed=len(failures), failures=failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
