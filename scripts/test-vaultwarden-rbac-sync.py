#!/usr/bin/env python3
"""vaultwarden-rbac-sync の sync.py (ConfigMapに埋め込み) を、実際の
Authentik/Vaultwarden API を呼ばずに検証するユニットテスト。

sync.py は gitops/manifests/prod/vaultwarden-rbac-sync/script-configmap.yaml の
ConfigMap data として管理されているため、YAMLから埋め込みソースを抽出して exec する。

使い方:
  python3 scripts/test-vaultwarden-rbac-sync.py
"""
import io
import json
import logging
import pathlib
import sys
import urllib.error
import urllib.parse

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_CONFIGMAP = (
    REPO_ROOT / "gitops/manifests/prod/vaultwarden-rbac-sync/script-configmap.yaml"
)

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


def assert_raises(desc, fn, exc_type):
    global PASS, FAIL
    try:
        fn()
    except exc_type:
        print(f"  ✅ {desc}")
        PASS += 1
    else:
        print(f"  ❌ {desc} (例外が発生しなかった)")
        FAIL += 1


def load_sync_module():
    doc = yaml.safe_load(SCRIPT_CONFIGMAP.read_text())
    source = doc["data"]["sync.py"]
    namespace = {"__name__": "sync_under_test"}
    exec(compile(source, "sync.py", "exec"), namespace)
    return namespace


sync = load_sync_module()
load_mapping = sync["load_mapping"]
MappingConfigError = sync["MappingConfigError"]
main = sync["main"]

print("=== MappingConfigLoader ユニットテスト (Requirement 4.1-4.4) ===")

valid_json = json.dumps(
    {
        "mappings": [
            {
                "authentik_group": "広報",
                "organization": "SNSアカウント",
                "collection": "広報",
                "permission": "can_view",
            },
            {
                "authentik_group": "広報",
                "organization": "口座情報",
                "collection": "広報",
                "permission": "can_edit",
            },
        ]
    }
)
entries = load_mapping(valid_json)
assert_true("正常なマッピングが全件ロードされる", len(entries) == 2)
assert_true(
    "同一グループが複数エントリを持てる (4.4)",
    {e.organization for e in entries} == {"SNSアカウント", "口座情報"},
)

assert_raises(
    "構文不正 (JSON parse error) で MappingConfigError (4.3)",
    lambda: load_mapping("{not valid json"),
    MappingConfigError,
)
assert_raises(
    "必須フィールド欠落で MappingConfigError (4.3)",
    lambda: load_mapping(json.dumps({"mappings": [{"authentik_group": "広報"}]})),
    MappingConfigError,
)
assert_raises(
    "未知の permission 値で MappingConfigError (4.2, 4.3)",
    lambda: load_mapping(
        json.dumps(
            {
                "mappings": [
                    {
                        "authentik_group": "広報",
                        "organization": "SNSアカウント",
                        "collection": "広報",
                        "permission": "can_delete",
                    }
                ]
            }
        )
    ),
    MappingConfigError,
)
assert_raises(
    "mappings がトップレベル配列でない場合 MappingConfigError (4.3)",
    lambda: load_mapping(json.dumps({"mappings": "not-a-list"})),
    MappingConfigError,
)

print("")
print("=== 実行エントリポイント骨格ユニットテスト (Requirement 10.1) ===")

root_logger = logging.getLogger()
for handler in list(root_logger.handlers):
    root_logger.removeHandler(handler)
buf = io.StringIO()
root_logger.addHandler(logging.StreamHandler(buf))
root_logger.setLevel(logging.INFO)

exit_code = main(["--mode=cron"])
log_output = buf.getvalue()
assert_true("--mode=cron 実行が正常終了する (exit 0)", exit_code == 0)
assert_true("ログにモード名 'cron' が記録される", '"mode": "cron"' in log_output)
assert_true("startupイベントがログに記録される", '"event": "startup"' in log_output)

print("")
print("=== AuthentikGroupClient ユニットテスト (Requirement 1.1-1.4) ===")

AuthentikGroupClient = sync["AuthentikGroupClient"]
AuthentikApiError = sync["AuthentikApiError"]


class _FakeAuthentikResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_urlopen_factory(responses_by_group_name, raises=None):
    def _fake_urlopen(request, timeout=None):
        if raises is not None:
            raise raises
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        requested_group = query.get("name", [""])[0]
        payload = responses_by_group_name.get(requested_group, {"results": []})
        return _FakeAuthentikResponse(payload)

    return _fake_urlopen


client = AuthentikGroupClient(base_url="https://auth.example.invalid", api_token="dummy-token")

sync["urlopen"] = _fake_urlopen_factory(
    {
        "広報": {
            "results": [
                {
                    "name": "広報",
                    "users_obj": [
                        {"pk": 1, "username": "alice", "email": "alice@example.invalid"},
                        {"pk": 2, "username": "bob", "email": "bob@example.invalid"},
                    ],
                }
            ]
        }
    }
)
result = client.get_group_members("広報")
assert_true(
    "存在するグループのメンバーemailが取得できる (1.1)",
    set(result.member_emails) == {"alice@example.invalid", "bob@example.invalid"},
)
assert_true("存在するグループはerrorがNone", result.error is None)

sync["urlopen"] = _fake_urlopen_factory({})
result = client.get_group_members("存在しないグループ")
assert_true("存在しないグループはmember_emailsが空", result.member_emails == [])
assert_true(
    "存在しないグループは例外を投げずerrorに記録され処理を継続できる (1.4)",
    result.error is not None,
)

sync["urlopen"] = _fake_urlopen_factory(
    {}, raises=urllib.error.HTTPError("https://auth.example.invalid", 401, "Unauthorized", None, None)
)
assert_raises(
    "認証エラー(401)はAuthentikApiErrorとして上位に伝播する (1.2, 1.3)",
    lambda: client.get_group_members("広報"),
    AuthentikApiError,
)

sync["urlopen"] = _fake_urlopen_factory({}, raises=TimeoutError("timed out"))
assert_raises(
    "タイムアウトはAuthentikApiErrorとして上位に伝播する (1.3)",
    lambda: client.get_group_members("広報"),
    AuthentikApiError,
)

print("")
print(f"{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
