#!/usr/bin/env python3
import importlib.util
import json
import os
import sys

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
mock_responses = {}  # path -> list of (status, payload), consumed in order


def fake_urlopen(request, timeout=None):
    path = request.full_url.split("cluster.local:8080", 1)[-1]
    body = json.loads(request.data.decode("utf-8")) if request.data else None
    mock_requests.append({"path": path, "method": request.method, "body": body})

    queue = mock_responses.get(path, [])
    status, payload = queue.pop(0) if queue else (200, {})
    if status >= 400:
        import urllib.error

        raise urllib.error.HTTPError(
            request.full_url, status, "err", {}, __import__("io").BytesIO(json.dumps(payload).encode())
        )
    return FakeResponse(payload, status)


def load_module():
    spec = importlib.util.spec_from_file_location(
        "zitadel_invite_migration",
        os.path.join(os.path.dirname(__file__), "zitadel-invite-migration.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def reset():
    mock_requests.clear()
    mock_responses.clear()


def run_tests():
    mod = load_module()
    mod.urlopen = fake_urlopen

    BASE = "http://zitadel.zitadel.svc.cluster.local:8080"
    DOMAIN = "zitadel.zitadel.svc.cluster.local"
    PAT = "test-pat"

    print("find_user_id: 見つかる場合")
    reset()
    mock_responses["/v2/users"] = [(200, {"result": [{"userId": "u1"}]})]
    uid = mod.find_user_id(BASE, DOMAIN, PAT, "a@example.com")
    assert_true("userIdを返す", uid == "u1")

    print("find_user_id: 見つからない場合")
    reset()
    mock_responses["/v2/users"] = [(200, {"result": []})]
    uid = mod.find_user_id(BASE, DOMAIN, PAT, "a@example.com")
    assert_true("Noneを返す", uid is None)

    print("ensure_user: 新規作成")
    reset()
    mock_responses["/v2/users"] = [(200, {"result": []})]
    mock_responses["/v2/users/human"] = [(200, {"userId": "u2"})]
    uid, created = mod.ensure_user(BASE, DOMAIN, PAT, "b@example.com", "B", "Test")
    assert_true("新規userIdを返す", uid == "u2")
    assert_true("created=True", created is True)

    print("ensure_user: 既存ユーザーはスキップ")
    reset()
    mock_responses["/v2/users"] = [(200, {"result": [{"userId": "u3"}]})]
    uid, created = mod.ensure_user(BASE, DOMAIN, PAT, "c@example.com", "C", "Test")
    assert_true("既存userIdを返す", uid == "u3")
    assert_true("created=False", created is False)
    assert_true("AddHumanUserを呼ばない", not any(r["path"] == "/v2/users/human" for r in mock_requests))

    print("ensure_user: 409(ALREADY_EXISTS)は再検索して継続")
    reset()
    mock_responses["/v2/users"] = [(200, {"result": []}), (200, {"result": [{"userId": "u4"}]})]
    mock_responses["/v2/users/human"] = [(409, {"code": 6})]
    uid, created = mod.ensure_user(BASE, DOMAIN, PAT, "d@example.com", "D", "Test")
    assert_true("再検索で見つけたuserIdを返す", uid == "u4")
    assert_true("created=False(既に存在)", created is False)

    print("ensure_grant: 新規付与")
    reset()
    mock_responses["/management/v1/users/grants/_search"] = [(200, {"result": []})]
    mock_responses["/management/v1/users/u5/grants"] = [(200, {"userGrantId": "g1"})]
    result = mod.ensure_grant(BASE, DOMAIN, PAT, "u5", "p1", ["planning"])
    assert_true("createdを返す", result == "created")

    print("ensure_grant: 既存grantはスキップ")
    reset()
    mock_responses["/management/v1/users/grants/_search"] = [
        (200, {"result": [{"projectId": "p1", "roleKeys": ["planning", "leader"]}]})
    ]
    result = mod.ensure_grant(BASE, DOMAIN, PAT, "u6", "p1", ["planning"])
    assert_true("already_grantedを返す", result == "already_granted")
    assert_true("grant作成APIを呼ばない", not any("/grants" in r["path"] and r["method"] == "POST" and r["path"] != "/management/v1/users/grants/_search" for r in mock_requests))

    print("issue_invite_code: returnCode")
    reset()
    mock_responses["/v2/users/u7/invite_code"] = [(200, {"inviteCode": "ABC123"})]
    code = mod.issue_invite_code(BASE, DOMAIN, PAT, "u7", send_email=False)
    assert_true("inviteCodeを返す", code == "ABC123")
    assert_true("returnCodeを送信した", mock_requests[-1]["body"] == {"returnCode": {}})

    print("issue_invite_code: sendCode")
    reset()
    mock_responses["/v2/users/u8/invite_code"] = [(200, {})]
    mod.issue_invite_code(BASE, DOMAIN, PAT, "u8", send_email=True)
    assert_true("sendCodeを送信した", mock_requests[-1]["body"] == {"sendCode": {}})

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    run_tests()
