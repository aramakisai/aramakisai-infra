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


# Global variables for tracking calls
mock_requests = []
mock_raises = None


def fake_urlopen(request, timeout=None):
    if mock_raises:
        raise mock_raises

    url = request.full_url
    method = request.method
    body = None
    if request.data:
        body = json.loads(request.data.decode("utf-8"))

    mock_requests.append({"url": url, "method": method, "body": body, "headers": request.headers})

    if "/api/v3/core/users/" in url and method == "GET":
        return FakeResponse(
            {
                "results": [
                    {
                        "pk": 1,
                        "email": "already_sent@example.com",
                        "attributes": {
                            "exhibitor_recovery_sent_at": "2024-01-01T00:00:00Z",
                            "keep_me": 1,
                        },
                    },
                    {"pk": 2, "email": "fail_user@example.com", "attributes": {"keep_me": 2}},
                    {"pk": 3, "email": "success_user@example.com", "attributes": {"keep_me": 3}},
                ]
            }
        )
    elif "recovery_email" in url and method == "POST":
        # Simulate failure for fail_user
        if "users/2/" in url:
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)
        return FakeResponse({}, status=204)
    elif method == "PATCH":
        return FakeResponse({})

    return FakeResponse({})


# Load sender script
script_path = os.path.join(os.path.dirname(__file__), "send-student-exhibitor-recovery-emails.py")
spec = importlib.util.spec_from_file_location("sender", script_path)
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)

orig_environ = os.environ.copy()
orig_argv = sys.argv.copy()
orig_urlopen = sender.urlopen


def reset_test_state():
    global mock_requests, mock_raises
    mock_requests = []
    mock_raises = None
    os.environ["AUTHENTIK_URL"] = "https://idp.example.com"
    os.environ["STUDENT_EXHIBITOR_RECOVERY_AUTHENTIK_TOKEN"] = "dummy-token"
    os.environ["STUDENT_EXHIBITOR_EMAIL_STAGE_ID"] = "dummy-stage-id"
    sender.urlopen = fake_urlopen


print("=== Unit tests for send-student-exhibitor-recovery-emails.py ===")

try:
    # Test 1: Normal run handling already sent, failure, and success correctly
    reset_test_state()
    sys.argv = ["send-student-exhibitor-recovery-emails.py"]

    try:
        sender.main()
    except SystemExit as e:
        # Expected to exit with 1 because of the simulated failure
        assert_true("main() exits 1 if any user failed", e.code == 1)

    # Analyze recorded requests
    post_requests = [r for r in mock_requests if r["method"] == "POST"]
    patch_requests = [r for r in mock_requests if r["method"] == "PATCH"]

    assert_true(
        "Already-sent filter correctly skips users with exhibitor_recovery_sent_at set",
        all("users/1/" not in r["url"] for r in post_requests),
    )

    assert_true(
        "A simulated failure for one user does not stop processing of the next user",
        any("users/3/" in r["url"] for r in post_requests),
    )

    # Check PATCH body for success_user (pk=3)
    success_patch = next(r for r in patch_requests if "users/3/" in r["url"])
    attributes = success_patch["body"]["attributes"]
    assert_true(
        "Attributes PATCH body preserves pre-existing unrelated attribute keys",
        attributes.get("keep_me") == 3 and "exhibitor_recovery_sent_at" in attributes,
    )

    assert_true(
        "All requests send Accept-Language: ja (User.locale() prioritizes request.LANGUAGE_CODE "
        "over attributes.settings.locale)",
        all(r.get("headers", {}).get("Accept-language") == "ja" for r in mock_requests),
    )

    # Test 2: Dry-run makes zero mutating calls
    reset_test_state()
    sys.argv = ["send-student-exhibitor-recovery-emails.py", "--dry-run"]

    try:
        sender.main()
    except SystemExit as e:
        assert_true("main() exits 0 when --dry-run completes without failure", e.code == 0)

    mutating_requests = [r for r in mock_requests if r["method"] in ("POST", "PATCH")]
    assert_true("--dry-run makes zero mutating calls", len(mutating_requests) == 0)

finally:
    # Restore original state
    os.environ.clear()
    os.environ.update(orig_environ)
    sys.argv = orig_argv
    sender.urlopen = orig_urlopen

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
