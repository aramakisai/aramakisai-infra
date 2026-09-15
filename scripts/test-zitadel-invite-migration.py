#!/usr/bin/env python3
import importlib.util
import json
import os
import sys
import urllib.error

PASS = 0
FAIL = 0


def assert_true(desc, cond):
    global PASS, FAIL
    if cond:
        print(f"  ✅ {desc}")
        PASS += 1
    else:
        print(f"  ❌ {desc}")
        FAIL += 1


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


mock_requests = []
# username -> existing userId, to simulate "user already exists" (HTTP 409) on create
existing_usernames = {}
# userId -> True, to simulate "grant already exists" (HTTP 409)
granted_user_ids = set()


def fake_urlopen(request, timeout=None):
    url = request.full_url
    method = request.method
    body = json.loads(request.data.decode("utf-8")) if request.data else None
    mock_requests.append({"url": url, "method": method, "body": body})

    if url.endswith("/v2/users/human") and method == "POST":
        username = body["username"]
        if username in existing_usernames:
            raise urllib.error.HTTPError(
                url, 409, "Conflict", {},
                _err_body({"code": 6, "message": "User already exists (V3-DKcYh)"}),
            )
        return FakeResponse({"userId": f"new-{username}", "details": {}})

    if url.endswith("/v2/users") and method == "POST":
        # find_user_id_by_username lookup after a 409
        wanted = body["queries"][0]["userNameQuery"]["userName"]
        if wanted in existing_usernames:
            return FakeResponse(
                {"result": [{"userId": existing_usernames[wanted], "username": wanted}]}
            )
        return FakeResponse({"result": []})

    if "/grants" in url and method == "POST":
        user_id = url.rsplit("/users/", 1)[1].split("/grants")[0]
        if user_id in granted_user_ids:
            raise urllib.error.HTTPError(
                url, 409, "Conflict", {},
                _err_body({"code": 6, "message": "User grant already exists (V3-DKcYh)"}),
            )
        return FakeResponse({"userGrantId": f"grant-{user_id}"})

    if "/invite_code" in url and method == "POST":
        user_id = url.rsplit("/users/", 1)[1].split("/invite_code")[0]
        if "sendCode" in body:
            return FakeResponse({"details": {}})
        return FakeResponse({"details": {}, "inviteCode": f"CODE-{user_id}"})

    raise AssertionError(f"unexpected request: {method} {url}")


def _err_body(payload):
    import io

    return io.BytesIO(json.dumps(payload).encode("utf-8"))


script_path = os.path.join(os.path.dirname(__file__), "zitadel-invite-migration.py")
spec = importlib.util.spec_from_file_location("migrator", script_path)
migrator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrator)

orig_environ = os.environ.copy()
orig_argv = sys.argv.copy()
orig_urlopen = migrator.urlopen


def reset_test_state():
    global mock_requests, existing_usernames, granted_user_ids
    mock_requests = []
    existing_usernames = {}
    granted_user_ids = set()
    os.environ["ZITADEL_DOMAIN"] = "http://zitadel.example.internal:8080"
    os.environ["ZITADEL_PROJECT_ID"] = "proj-1"
    os.environ["ZITADEL_TOKEN"] = "dummy-token"
    migrator.urlopen = fake_urlopen


print("=== Unit tests for zitadel-invite-migration.py ===")

try:
    # Test 1: normal run creates users, grants roles, issues invite codes (returnCode mode)
    reset_test_state()
    sys.argv = ["zitadel-invite-migration.py"]

    try:
        migrator.main()
    except SystemExit as e:
        assert_true("main() exits 0 when all users succeed", e.code == 0)

    create_reqs = [r for r in mock_requests if r["url"].endswith("/v2/users/human")]
    assert_true(
        "Creates one human user per SAMPLE_USERS entry",
        len(create_reqs) == len(migrator.SAMPLE_USERS),
    )
    assert_true(
        "AddHumanUser body sets email.isVerified but no password field",
        all("password" not in r["body"] and r["body"]["email"]["isVerified"] is True for r in create_reqs),
    )

    grant_reqs = [r for r in mock_requests if "/grants" in r["url"]]
    multi_group_user = next(u for u in migrator.SAMPLE_USERS if len(u["groups"]) == 2)
    multi_group_role_keys = [migrator.GROUP_DISPLAY_TO_ROLE_KEY[g] for g in multi_group_user["groups"]]
    assert_true(
        "Multi-group user gets a single grant call listing every mapped role_key",
        any(sorted(r["body"]["roleKeys"]) == sorted(multi_group_role_keys) for r in grant_reqs),
    )

    invite_reqs = [r for r in mock_requests if "/invite_code" in r["url"]]
    assert_true(
        "Default mode requests returnCode (no SMTP dependency)",
        all(r["body"] == {"returnCode": {}} for r in invite_reqs),
    )

    # Test 2: re-run is idempotent — 409 on create/grant is treated as already-done, not a failure
    reset_test_state()
    first_user = migrator.SAMPLE_USERS[0]
    existing_usernames[first_user["email"]] = "existing-user-id"
    granted_user_ids.add("existing-user-id")
    sys.argv = ["zitadel-invite-migration.py"]

    try:
        migrator.main()
    except SystemExit as e:
        assert_true("Re-run with pre-existing user/grant still exits 0 (idempotent)", e.code == 0)

    reused_invite_reqs = [r for r in mock_requests if "/invite_code" in r["url"] and "existing-user-id" in r["url"]]
    assert_true(
        "Invite code is still (re-)issued for an already-existing user",
        len(reused_invite_reqs) == 1,
    )

    # Test 3: unknown group is skipped, not fatal
    reset_test_state()
    sys.argv = ["zitadel-invite-migration.py", "--input", "unused.json"]
    migrator.load_users = lambda _path: [
        {"email": "x@example.invalid", "given_name": "X", "family_name": "Y", "groups": ["存在しないグループ"]}
    ]
    try:
        migrator.main()
    except SystemExit as e:
        assert_true("Unknown group is logged and skipped, run still succeeds", e.code == 0)
    unknown_grant_reqs = [r for r in mock_requests if "/grants" in r["url"]]
    assert_true("No grant call is made when every group is unmapped", len(unknown_grant_reqs) == 0)

    # Test 4: --dry-run makes zero network calls
    reset_test_state()
    migrator.load_users = lambda _path: migrator.SAMPLE_USERS
    sys.argv = ["zitadel-invite-migration.py", "--dry-run"]
    try:
        migrator.main()
    except SystemExit as e:
        assert_true("--dry-run exits 0", e.code == 0)
    assert_true("--dry-run makes zero API calls", len(mock_requests) == 0)

    # Test 5: --send-email switches invite_code body to sendCode
    reset_test_state()
    migrator.load_users = lambda _path: migrator.SAMPLE_USERS
    sys.argv = ["zitadel-invite-migration.py", "--send-email"]
    try:
        migrator.main()
    except SystemExit as e:
        assert_true("--send-email exits 0", e.code == 0)
    send_email_reqs = [r for r in mock_requests if "/invite_code" in r["url"]]
    assert_true(
        "--send-email requests sendCode instead of returnCode",
        all(r["body"] == {"sendCode": {}} for r in send_email_reqs),
    )

finally:
    os.environ.clear()
    os.environ.update(orig_environ)
    sys.argv = orig_argv
    migrator.urlopen = orig_urlopen

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
