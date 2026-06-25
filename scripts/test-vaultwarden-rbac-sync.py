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
                "collection_id": "3d64cf3a-fc4c-4004-a9c7-9967c008ac38",
                "collection_label": "広報",
                "permission": "can_view",
            },
            {
                "authentik_group": "広報",
                "organization": "口座情報",
                "collection_id": "05a550e7-7f43-466f-bf84-a98a7034dc10",
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
assert_true(
    "collection_idがそのまま保持される (Collection名は暗号化CipherStringのためID直接指定方式, research.md参照)",
    {e.collection_id for e in entries}
    == {"3d64cf3a-fc4c-4004-a9c7-9967c008ac38", "05a550e7-7f43-466f-bf84-a98a7034dc10"},
)
assert_true(
    "collection_labelは任意フィールド (省略可)",
    [e.collection_label for e in entries] == ["広報", None],
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
                        "collection_id": "3d64cf3a-fc4c-4004-a9c7-9967c008ac38",
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
print("=== VaultwardenOrgClient.authenticate ユニットテスト (Requirement 2.1-2.4) ===")

VaultwardenOrgClient = sync["VaultwardenOrgClient"]
VaultwardenAuthError = sync["VaultwardenAuthError"]


class _FakeVaultwardenResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _fake_token_urlopen_factory(payload=None, raises=None):
    def _fake_urlopen(request, timeout=None):
        if raises is not None:
            raise raises
        body = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert body["grant_type"] == ["client_credentials"]
        assert body["client_id"] == ["user.dummy-uuid"]
        assert body["client_secret"] == ["dummy-secret"]
        assert body["scope"] == ["api"]
        return _FakeVaultwardenResponse(payload if payload is not None else {})

    return _fake_urlopen


org_client = VaultwardenOrgClient(
    base_url="https://vault.example.invalid",
    client_id="user.dummy-uuid",
    client_secret="dummy-secret",
)

sync["urlopen"] = _fake_token_urlopen_factory({"access_token": "dummy-access-token", "token_type": "Bearer"})
token = org_client.authenticate()
assert_true("有効な認証情報でaccess_tokenが取得できる (2.1, 2.2)", token == "dummy-access-token")

sync["urlopen"] = _fake_token_urlopen_factory(
    raises=urllib.error.HTTPError("https://vault.example.invalid", 400, "invalid_client", None, None)
)
assert_raises(
    "認証失敗(400)はVaultwardenAuthErrorとして上位に伝播する (2.3)",
    lambda: org_client.authenticate(),
    VaultwardenAuthError,
)

sync["urlopen"] = _fake_token_urlopen_factory(raises=TimeoutError("timed out"))
assert_raises(
    "タイムアウトはVaultwardenAuthErrorとして上位に伝播する (2.3)",
    lambda: org_client.authenticate(),
    VaultwardenAuthError,
)

sync["urlopen"] = _fake_token_urlopen_factory({})
assert_raises(
    "access_tokenを含まないレスポンスはVaultwardenAuthError (2.3)",
    lambda: org_client.authenticate(),
    VaultwardenAuthError,
)

print("")
print("=== VaultwardenOrgClient 現状取得ユニットテスト (Requirement 3.1-3.3) ===")

VaultwardenApiError = sync["VaultwardenApiError"]
CollectionGrant = sync["CollectionGrant"]
Member = sync["Member"]


def _fake_org_api_urlopen_factory(responses_by_path, raises_by_path=None):
    def _fake_urlopen(request, timeout=None):
        path = urllib.parse.urlsplit(request.full_url).path
        if raises_by_path and path in raises_by_path:
            raise raises_by_path[path]
        assert request.headers.get("Authorization") == "Bearer dummy-access-token"
        return _FakeVaultwardenResponse(responses_by_path[path])

    return _fake_urlopen


state_client = VaultwardenOrgClient(
    base_url="https://vault.example.invalid",
    client_id="user.dummy-uuid",
    client_secret="dummy-secret",
)
state_client._access_token = "dummy-access-token"

sync["urlopen"] = _fake_org_api_urlopen_factory(
    {
        "/api/organizations": {
            "data": [
                {"id": "org-1", "name": "2.encrypted-org-name=="},
                {"id": "org-2", "name": "2.another-encrypted-name=="},
            ]
        }
    }
)
orgs = state_client.list_organizations()
assert_true("Organization一覧が取得できる (3.1)", [o.org_id for o in orgs] == ["org-1", "org-2"])

sync["urlopen"] = _fake_org_api_urlopen_factory(
    {
        "/api/organizations/org-1/users": {
            "data": [
                {
                    "id": "member-1",
                    "email": "alice@example.invalid",
                    "type": 1,
                    "status": 2,
                    "collections": [
                        {"id": "col-1", "readOnly": True, "hidePasswords": False, "manage": False},
                    ],
                },
                {
                    "id": "member-2",
                    "email": "bob@example.invalid",
                    "type": 2,
                    "status": 0,
                    "collections": [],
                },
            ]
        },
        "/api/organizations/org-1/collections": {
            "data": [
                {"id": "col-1", "name": "2.encrypted-collection-name=="},
                {"id": "col-2", "name": "2.another-encrypted-collection=="},
            ]
        },
    }
)
org_state = state_client.get_org_state("org-1")
assert_true(
    "メンバー一覧（メール・メンバーID・現在のCollection権限）が取得できる (3.1)",
    [(m.member_id, m.email, m.member_type, m.status) for m in org_state.members]
    == [("member-1", "alice@example.invalid", 1, 2), ("member-2", "bob@example.invalid", 2, 0)],
)
assert_true(
    "メンバーの現在のCollection権限が取得できる (3.1)",
    org_state.members[0].collections == [CollectionGrant("col-1", True, False, False)],
)
assert_true("Collection一覧が取得できる (3.2)", [c.collection_id for c in org_state.collections] == ["col-1", "col-2"])

assert_true(
    "マッピングが参照するcollection_idが存在する場合はそのIDを返す (3.2)",
    state_client.resolve_mapping_collection(org_state, "col-1") == "col-1",
)
assert_true(
    "マッピングが参照するcollection_idが対象Organizationに存在しない場合はNoneを返しエラー記録に使える (3.3)",
    state_client.resolve_mapping_collection(org_state, "col-nonexistent") is None,
)

sync["urlopen"] = _fake_org_api_urlopen_factory(
    {},
    raises_by_path={
        "/api/organizations/org-broken/users": urllib.error.HTTPError(
            "https://vault.example.invalid", 500, "Internal Server Error", None, None
        )
    },
)
assert_raises(
    "Organization状態取得のHTTPエラーはVaultwardenApiErrorとして上位に伝播する",
    lambda: state_client.get_org_state("org-broken"),
    VaultwardenApiError,
)

print("")
print("=== VaultwardenOrgClient 招待・権限更新・削除ユニットテスト (Requirement 6, 7, 8) ===")

permission_to_collection_grant = sync["permission_to_collection_grant"]
merge_collection_grants = sync["merge_collection_grants"]
remove_collection_grants = sync["remove_collection_grants"]
filter_confirm_pending = sync["filter_confirm_pending"]

assert_true(
    "can_view は readOnly=True/hidePasswords=False/manage=False に変換される",
    permission_to_collection_grant("col-1", "can_view") == CollectionGrant("col-1", True, False, False),
)
assert_true(
    "can_view_except_passwords は readOnly=True/hidePasswords=True/manage=False に変換される",
    permission_to_collection_grant("col-1", "can_view_except_passwords")
    == CollectionGrant("col-1", True, True, False),
)
assert_true(
    "can_edit は readOnly=False/hidePasswords=False/manage=False に変換される",
    permission_to_collection_grant("col-1", "can_edit") == CollectionGrant("col-1", False, False, False),
)
assert_true(
    "can_manage は readOnly=False/hidePasswords=False/manage=True に変換される",
    permission_to_collection_grant("col-1", "can_manage") == CollectionGrant("col-1", False, False, True),
)

current_grants = [
    CollectionGrant("col-unmapped-1", True, False, False),
    CollectionGrant("col-mapped", False, False, False),
    CollectionGrant("col-unmapped-2", True, True, False),
]
desired_grants = [CollectionGrant("col-mapped", False, False, True), CollectionGrant("col-new", True, False, False)]
merged = merge_collection_grants(current_grants, desired_grants)
merged_by_id = {g.collection_id: g for g in merged}
assert_true(
    "マッピング対象外のCollection権限はマージ前後で変化しない (7.1, 7.2)",
    merged_by_id["col-unmapped-1"] == CollectionGrant("col-unmapped-1", True, False, False)
    and merged_by_id["col-unmapped-2"] == CollectionGrant("col-unmapped-2", True, True, False),
)
assert_true(
    "マッピング対象のCollection権限は新しい値に更新される (7.1)",
    merged_by_id["col-mapped"] == CollectionGrant("col-mapped", False, False, True),
)
assert_true("マッピングにより新規追加されたCollectionも反映される (7.1)", merged_by_id["col-new"].manage is False)
assert_true("マージ後の件数は元の権限数+新規追加数 (7.2)", len(merged) == 4)

removed = remove_collection_grants(current_grants, {"col-mapped"})
assert_true(
    "脱退したグループに対応するCollection権限のみ削除される (8.1, 8.2)",
    {g.collection_id for g in removed} == {"col-unmapped-1", "col-unmapped-2"},
)
assert_true(
    "削除対象外のCollection権限は変化しない (8.2)",
    CollectionGrant("col-unmapped-1", True, False, False) in removed,
)

pending_members = [
    Member("member-1", "alice@example.invalid", 1, 2, []),  # status=2 (Confirmed)
    Member("member-2", "bob@example.invalid", 2, 0, []),  # status=0 (Invited)
    Member("member-3", "carol@example.invalid", 2, 1, []),  # status=1 (Accepted, 未Confirm)
]
pending = filter_confirm_pending(pending_members)
assert_true(
    "Confirmed以外のメンバーがConfirm待ちとして抽出される (6.5)",
    {m.member_id for m in pending} == {"member-2", "member-3"},
)

invite_calls = []


def _fake_invite_urlopen(request, timeout=None):
    invite_calls.append(json.loads(request.data.decode("utf-8")))
    assert request.headers.get("Authorization") == "Bearer dummy-access-token"
    return _FakeVaultwardenResponse({})


sync["urlopen"] = _fake_invite_urlopen
state_client.invite_member(
    "org-1", "jiro@example.invalid", member_type=2, collections=[CollectionGrant("col-1", True, False, False)]
)
assert_true(
    "招待APIにemail/type/collectionsが送信される (6.1, 6.2)",
    invite_calls[0]["emails"] == ["jiro@example.invalid"]
    and invite_calls[0]["type"] == 2
    and invite_calls[0]["collections"] == [{"id": "col-1", "readOnly": True, "hidePasswords": False, "manage": False}],
)

sync["urlopen"] = _fake_org_api_urlopen_factory(
    {},
    raises_by_path={
        "/api/organizations/org-1/users/invite": urllib.error.HTTPError(
            "https://vault.example.invalid", 400, "Bad Request", None, None
        )
    },
)
assert_raises(
    "招待APIのエラーはVaultwardenApiErrorとして上位に伝播し、呼び出し元で当該ユーザーのみエラー記録できる (6.3)",
    lambda: state_client.invite_member("org-1", "broken@example.invalid", member_type=2, collections=[]),
    VaultwardenApiError,
)

put_calls = []


def _fake_put_urlopen(request, timeout=None):
    put_calls.append(json.loads(request.data.decode("utf-8")))
    return _FakeVaultwardenResponse({})


sync["urlopen"] = _fake_put_urlopen
state_client.put_member_collections(
    "org-1",
    "member-1",
    member_type=1,
    collections=[CollectionGrant("col-1", True, False, False), CollectionGrant("col-2", False, False, True)],
)
assert_true(
    "PUTのtypeには対象メンバーの現在のtypeがそのまま再送される (7.3、型変更を伴わない)",
    put_calls[0]["type"] == 1,
)
assert_true(
    "PUTのcollectionsには指定した全Collection権限が送信される (7.1, 7.2)",
    put_calls[0]["collections"]
    == [
        {"id": "col-1", "readOnly": True, "hidePasswords": False, "manage": False},
        {"id": "col-2", "readOnly": False, "hidePasswords": False, "manage": True},
    ],
)
assert_true("PUTのgroupsは常に空配列 (OSSはEnterprise Groups未実装)", put_calls[0]["groups"] == [])

print("")
print(f"{PASS} passed, {FAIL} failed")
sys.exit(0 if FAIL == 0 else 1)
