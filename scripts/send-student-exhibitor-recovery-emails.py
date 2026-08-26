#!/usr/bin/env python3
import argparse
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


# Cloudflare WAF が urllib デフォルトUA (Python-urllib/x.y) を bot として
# ブロックする (error code 1010) ため、識別可能なUAを明示的に設定する。
_UA = "aramakisai-infra/send-student-exhibitor-recovery-emails"


def make_request(
    url: str, token: str, method: str = "GET", body: dict | None = None
) -> dict | None:
    # User.locale(request)はattributes.settings.localeよりrequest.LANGUAGE_CODE
    # (Accept-Languageヘッダ由来)を優先する実装のため、これがないとメール本文が
    # 英語になる(実機検証、2026.8.0で確認・2026-08-26)。
    headers = {"Authorization": f"Bearer {token}", "User-Agent": _UA, "Accept-Language": "ja"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, timeout=10) as resp:
        if resp.status == 204:
            return None
        raw = resp.read()
        if raw:
            return json.loads(raw)
        return None


def main():
    parser = argparse.ArgumentParser(description="Send student exhibitor recovery emails.")
    parser.add_argument(
        "--dry-run", action="store_true", help="List target users without making mutating API calls."
    )
    parser.add_argument("--user", type=str, help="Force-process a single user by email.")
    args = parser.parse_args()

    base_url = os.environ.get("AUTHENTIK_URL", "https://idp.aramakisai.com").rstrip("/")
    api_token = get_env_or_die("STUDENT_EXHIBITOR_RECOVERY_AUTHENTIK_TOKEN")
    email_stage_id = get_env_or_die("STUDENT_EXHIBITOR_EMAIL_STAGE_ID")

    log_event("startup", mode="dry-run" if args.dry_run else "execute", target_user=args.user)

    users_url = f"{base_url}/api/v3/core/users/?groups_by_name=student_exhibitor&page_size=500"
    # 想定される出展団体数は上限500件未満のため、単一ページでの取得で網羅可能。
    try:
        users_resp = make_request(users_url, api_token)
    except (HTTPError, URLError, TimeoutError) as exc:
        log_event("fetch_users_failed", error=str(exc))
        sys.exit(1)

    all_users = users_resp.get("results", [])

    skipped_count = 0
    success_count = 0
    failures = []

    for user in all_users:
        email = user.get("email")
        if not email:
            continue

        attributes = user.get("attributes", {}) or {}

        if args.user and email != args.user:
            continue

        if not args.user and attributes.get("exhibitor_recovery_sent_at"):
            skipped_count += 1
            continue

        if args.dry_run:
            log_event("dry_run_target", email=email, user_id=user["pk"])
            success_count += 1
            continue

        user_id = user["pk"]
        try:
            recovery_url = f"{base_url}/api/v3/core/users/{user_id}/recovery_email/"
            make_request(
                recovery_url, api_token, method="POST", body={"email_stage": email_stage_id}
            )

            # Authentikの属性(attributes)はJSONFieldであり、PATCH時にディープマージされず既存キーごと上書きされる。
            # 他のシステムやフローが記録した未知の属性を消失させないため、必ず最新の取得値をベースにマージする。
            now_iso = datetime.now(timezone.utc).isoformat()
            updated_attributes = attributes.copy()
            updated_attributes["exhibitor_recovery_sent_at"] = now_iso

            patch_url = f"{base_url}/api/v3/core/users/{user_id}/"
            make_request(
                patch_url, api_token, method="PATCH", body={"attributes": updated_attributes}
            )

            log_event("email_sent", email=email)
            success_count += 1
        except (HTTPError, URLError, TimeoutError) as exc:
            err_msg = str(exc)
            if isinstance(exc, HTTPError):
                try:
                    err_body = exc.read().decode("utf-8")
                    err_msg = f"HTTP {exc.code}: {err_body}"
                except Exception as decode_exc:
                    log_event("error_body_decode_failed", error=str(decode_exc))
            log_event("email_failed", email=email, error=err_msg)
            failures.append({"email": email, "error": err_msg})

    log_event(
        "summary",
        skipped=skipped_count,
        succeeded=success_count,
        failed=len(failures),
        failures=failures,
    )

    if failures:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
