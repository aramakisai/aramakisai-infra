#!/usr/bin/env python3
"""既存ユーザーをZitadelへ作成し招待コードを発行する (task6.1/task9.4)。

入力CSV (ヘッダ行必須): email,given_name,family_name,role_keys
  role_keys はセミコロン区切りで複数指定可 (例: planning;leader)。

冪等性: AddHumanUser/grant付与は既存確認してから実行するため再実行可能。
招待コードは再実行のたびに新規発行され旧コードは無効化される
(Zitadel v2 API仕様、design.md「招待オンボーディングフロー」参照)。
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def log_event(event: str, **kwargs):
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **kwargs}
    print(json.dumps(record, ensure_ascii=False))


def get_env_or_die(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        log_event("config_error", error=f"Missing required environment variable: {key}")
        sys.exit(1)
    return val


def api_request(
    base_url: str, external_domain: str, pat: str, path: str, method: str = "GET", body: dict | None = None
) -> tuple[int, dict]:
    # ZitadelはHostヘッダでインスタンスを解決するため、接続先(base_url)とは独立に
    # このヘッダのみを対象インスタンスのZITADEL_EXTERNALDOMAINへ一致させる
    # (ansible/roles/zitadel-bootstrap/files/zitadel_api.jsと同じ回避パターン。
    # base_url自体が既にexternal_domain宛の場合はこのヘッダは実質no-op)。
    headers = {
        "Authorization": f"Bearer {pat}",
        "Host": external_domain,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else {})


def resolve_project_id(base_url: str, external_domain: str, pat: str, project_name: str) -> str:
    status, resp = api_request(
        base_url, external_domain, pat, "/management/v1/projects/_search", method="POST", body={}
    )
    if status != 200:
        log_event("resolve_project_failed", status=status, response=resp)
        sys.exit(1)
    for project in resp.get("result", []):
        if project.get("name") == project_name:
            return project["id"]
    log_event("project_not_found", project_name=project_name)
    sys.exit(1)


def find_user_id(base_url: str, external_domain: str, pat: str, login_name: str) -> str | None:
    status, resp = api_request(
        base_url,
        external_domain,
        pat,
        "/v2/users",
        method="POST",
        body={"queries": [{"loginNameQuery": {"loginName": login_name}}]},
    )
    if status != 200:
        log_event("find_user_failed", login_name=login_name, status=status, response=resp)
        return None
    results = resp.get("result", [])
    return results[0]["userId"] if results else None


def ensure_user(
    base_url: str, external_domain: str, pat: str, email: str, given_name: str, family_name: str
) -> tuple[str, bool]:
    existing = find_user_id(base_url, external_domain, pat, email)
    if existing is not None:
        return existing, False

    status, resp = api_request(
        base_url,
        external_domain,
        pat,
        "/v2/users/human",
        method="POST",
        body={
            "username": email,
            "profile": {"givenName": given_name, "familyName": family_name},
            "email": {"email": email, "isVerified": True},
        },
    )
    if status not in (200, 201):
        # 409 (ALREADY_EXISTS) は並行実行やリトライで起こりうる。再検索して継続する。
        if status == 409:
            existing = find_user_id(base_url, external_domain, pat, email)
            if existing is not None:
                return existing, False
        log_event("create_user_failed", email=email, status=status, response=resp)
        raise RuntimeError(f"create_user_failed: {email}")
    return resp["userId"], True


def ensure_grant(
    base_url: str, external_domain: str, pat: str, user_id: str, project_id: str, role_keys: list[str]
) -> str:
    status, resp = api_request(
        base_url,
        external_domain,
        pat,
        "/management/v1/users/grants/_search",
        method="POST",
        body={"queries": [{"userIdQuery": {"userId": user_id}}]},
    )
    if status == 200:
        for grant in resp.get("result", []):
            if grant.get("projectId") == project_id:
                existing_roles = set(grant.get("roleKeys", []))
                if existing_roles >= set(role_keys):
                    return "already_granted"
                # 既存grantへの追加ロールは対象外(このスクリプトの対象は新規作成のみ、
                # 既存ユーザーのロール変更はtask9.2のresources.ymlのuser_grant管理範囲)
                return "already_granted_partial"

    status, resp = api_request(
        base_url,
        external_domain,
        pat,
        f"/management/v1/users/{user_id}/grants",
        method="POST",
        body={"projectId": project_id, "roleKeys": role_keys},
    )
    if status not in (200, 201):
        if status == 409:
            return "already_granted"
        log_event("create_grant_failed", user_id=user_id, status=status, response=resp)
        raise RuntimeError(f"create_grant_failed: {user_id}")
    return "created"


def issue_invite_code(
    base_url: str, external_domain: str, pat: str, user_id: str, send_email: bool
) -> str | None:
    body = {"sendCode": {}} if send_email else {"returnCode": {}}
    status, resp = api_request(
        base_url, external_domain, pat, f"/v2/users/{user_id}/invite_code", method="POST", body=body
    )
    if status not in (200, 201):
        log_event("invite_code_failed", user_id=user_id, status=status, response=resp)
        raise RuntimeError(f"invite_code_failed: {user_id}")
    return resp.get("inviteCode")


def main():
    parser = argparse.ArgumentParser(description="Migrate existing users into Zitadel with invite codes.")
    parser.add_argument("--csv", required=True, help="CSV: email,given_name,family_name,role_keys")
    parser.add_argument("--project-name", default="aramakisai", help="Zitadel project name for role grants.")
    parser.add_argument("--send-email", action="store_true", help="Send invite email (requires SMTP config).")
    parser.add_argument("--dry-run", action="store_true", help="List target users without mutating API calls.")
    args = parser.parse_args()

    base_url = os.environ.get("ZITADEL_API_BASE_URL", "http://zitadel.zitadel.svc.cluster.local:8080").rstrip(
        "/"
    )
    external_domain = os.environ.get("ZITADEL_EXTERNAL_DOMAIN", "zitadel.zitadel.svc.cluster.local")
    pat = get_env_or_die("ZITADEL_INVITE_RECOVERY_SA_PAT")

    log_event("startup", mode="dry-run" if args.dry_run else "execute", csv=args.csv)

    with open(args.csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if args.dry_run:
        for row in rows:
            log_event("dry_run_target", email=row["email"], role_keys=row["role_keys"])
        sys.exit(0)

    project_id = resolve_project_id(base_url, external_domain, pat, args.project_name)

    success_count = 0
    failures = []
    for row in rows:
        email = row["email"]
        role_keys = [r.strip() for r in row["role_keys"].split(";") if r.strip()]
        try:
            user_id, created = ensure_user(
                base_url, external_domain, pat, email, row["given_name"], row["family_name"]
            )
            grant_result = ensure_grant(base_url, external_domain, pat, user_id, project_id, role_keys)
            invite_code = issue_invite_code(base_url, external_domain, pat, user_id, args.send_email)
            log_event(
                "user_migrated",
                email=email,
                user_id=user_id,
                created=created,
                grant_result=grant_result,
                invite_code=invite_code if not args.send_email else "(sent by email)",
            )
            success_count += 1
        except RuntimeError as exc:
            failures.append({"email": email, "error": str(exc)})

    log_event("summary", succeeded=success_count, failed=len(failures), failures=failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
