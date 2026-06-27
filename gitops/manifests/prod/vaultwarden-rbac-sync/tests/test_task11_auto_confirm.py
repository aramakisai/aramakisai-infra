"""Task 11: 自動Confirm TDDテスト。

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task11_auto_confirm.py -v
"""
import base64
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Task 11.1 テスト: AutoConfirmPlan 計算 (PermissionDiffEngine)
# ---------------------------------------------------------------------------

class TestAutoConfirmPlan:
    """PermissionDiffEngine が status=1 メンバーを auto_confirm に追加することを確認する。"""

    def _make_state(self, members, collection_ids=None):
        from sync import OrgState, Collection
        collections = [Collection(cid, "") for cid in (collection_ids or [])]
        return OrgState(members=members, collections=collections)

    def test_accepted_member_added_to_auto_confirm(self):
        """status=1 (Accepted) メンバーは auto_confirm に含まれる。"""
        from sync import PermissionDiffEngine, MappingEntry, Member, CollectionGrant

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        member = Member(
            member_id="mem-1",
            email="accepted@example.com",
            member_type=2,
            status=1,  # Accepted
            collections=[],
            user_id="user-uuid-1",
        )
        state = self._make_state([member], ["coll-1"])

        plan = PermissionDiffEngine.compute_diff(
            mappings,
            {"部門A": ["accepted@example.com"]},
            {"TestOrg": state},
            {"TestOrg": "org-id-1"},
        )

        assert len(plan.auto_confirm) == 1
        assert plan.auto_confirm[0].email == "accepted@example.com"
        assert plan.auto_confirm[0].member_id == "mem-1"
        assert plan.auto_confirm[0].user_id == "user-uuid-1"
        assert plan.auto_confirm[0].org_id == "org-id-1"

    def test_invited_member_not_in_auto_confirm(self):
        """status=0 (Invited) メンバーは auto_confirm に含まれない。"""
        from sync import PermissionDiffEngine, MappingEntry, Member

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        member = Member(
            member_id="mem-1",
            email="invited@example.com",
            member_type=2,
            status=0,  # Invited — Accept前
            collections=[],
            user_id="user-uuid-1",
        )
        state = self._make_state([member], ["coll-1"])

        plan = PermissionDiffEngine.compute_diff(
            mappings,
            {"部門A": ["invited@example.com"]},
            {"TestOrg": state},
            {"TestOrg": "org-id-1"},
        )

        assert len(plan.auto_confirm) == 0

    def test_confirmed_member_not_in_auto_confirm(self):
        """status=2 (Confirmed) メンバーは auto_confirm に含まれない。"""
        from sync import PermissionDiffEngine, MappingEntry, Member

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        member = Member(
            member_id="mem-1",
            email="confirmed@example.com",
            member_type=2,
            status=2,  # Confirmed
            collections=[],
            user_id="user-uuid-1",
        )
        state = self._make_state([member], ["coll-1"])

        plan = PermissionDiffEngine.compute_diff(
            mappings,
            {"部門A": ["confirmed@example.com"]},
            {"TestOrg": state},
            {"TestOrg": "org-id-1"},
        )

        assert len(plan.auto_confirm) == 0

    def test_accepted_without_user_id_not_in_auto_confirm(self):
        """user_id 未設定の Accepted メンバーは auto_confirm に含まれない (公開鍵取得不可)。"""
        from sync import PermissionDiffEngine, MappingEntry, Member

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        member = Member(
            member_id="mem-1",
            email="accepted@example.com",
            member_type=2,
            status=1,
            collections=[],
            user_id="",  # 空
        )
        state = self._make_state([member], ["coll-1"])

        plan = PermissionDiffEngine.compute_diff(
            mappings,
            {"部門A": ["accepted@example.com"]},
            {"TestOrg": state},
            {"TestOrg": "org-id-1"},
        )

        assert len(plan.auto_confirm) == 0

    def test_confirm_pending_only_includes_invited_status0(self):
        """confirm_pending は status=0 (Accept待ち) のみ。status=1 は auto_confirm へ。"""
        from sync import PermissionDiffEngine, MappingEntry, Member

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        members = [
            Member("m-invited", "invited@example.com", 2, 0, [], user_id="uid-1"),
            Member("m-accepted", "accepted@example.com", 2, 1, [], user_id="uid-2"),
        ]
        state = self._make_state(members, ["coll-1"])

        plan = PermissionDiffEngine.compute_diff(
            mappings,
            {"部門A": ["invited@example.com", "accepted@example.com"]},
            {"TestOrg": state},
            {"TestOrg": "org-id-1"},
        )

        pending_emails = {p.email for p in plan.confirm_pending}
        assert "invited@example.com" in pending_emails
        assert "accepted@example.com" not in pending_emails
        assert len(plan.auto_confirm) == 1
        assert plan.auto_confirm[0].email == "accepted@example.com"


# ---------------------------------------------------------------------------
# Task 11.2 テスト: VaultwardenOrgClient.confirm_member
# ---------------------------------------------------------------------------

class TestConfirmMember:
    """VaultwardenOrgClient.confirm_member がRSA暗号化とAPI呼び出しを正しく行うことを確認する。"""

    def _make_client(self):
        from sync import VaultwardenOrgClient
        client = VaultwardenOrgClient(
            base_url="http://vaultwarden.test",
            client_id="user.test-uuid",
            client_secret="test-secret",
        )
        client._access_token = "fake-token"
        return client

    def test_confirm_member_calls_correct_endpoints(self):
        """confirm_member は /api/users/{userId}/public-key と /confirm を呼ぶ。"""
        from sync import VaultwardenOrgClient

        client = self._make_client()
        call_log = []

        def fake_request_json(method, path, body=None):
            call_log.append((method, path, body))
            if path.endswith("/public-key"):
                test_pub_key_b64 = _generate_test_rsa_pub_key_der_b64()
                return {"PublicKey": test_pub_key_b64}
            return {}

        client._request_json = fake_request_json

        org_key = b"\xab" * 64  # 64バイトのorg鍵

        with patch("sync._rsa_oaep_sha1_encrypt", return_value="encrypted-b64") as mock_enc:
            client.confirm_member(
                org_id="org-1",
                member_id="mem-1",
                user_id="user-uuid-1",
                org_key_bytes=org_key,
            )

        assert len(call_log) == 2
        get_call = call_log[0]
        assert get_call[0] == "GET"
        assert "user-uuid-1/public-key" in get_call[1]

        post_call = call_log[1]
        assert post_call[0] == "POST"
        assert "org-1/users/mem-1/confirm" in post_call[1]
        assert post_call[2]["key"] == "4.encrypted-b64"

    def test_confirm_member_uses_public_key_for_encryption(self):
        """confirm_member は取得した公開鍵で org_key を暗号化する。"""
        from sync import VaultwardenOrgClient

        client = self._make_client()
        test_pub_key_b64 = _generate_test_rsa_pub_key_der_b64()

        def fake_request_json(method, path, body=None):
            if path.endswith("/public-key"):
                return {"PublicKey": test_pub_key_b64}
            return {}

        client._request_json = fake_request_json
        org_key = b"\xcd" * 64

        encrypt_calls = []

        def mock_encrypt(pub_key_der_b64, plaintext):
            encrypt_calls.append((pub_key_der_b64, plaintext))
            return "mock-encrypted"

        with patch("sync._rsa_oaep_sha1_encrypt", side_effect=mock_encrypt):
            client.confirm_member("org-1", "mem-1", "uid-1", org_key)

        assert len(encrypt_calls) == 1
        assert encrypt_calls[0][0] == test_pub_key_b64
        assert encrypt_calls[0][1] == org_key


# ---------------------------------------------------------------------------
# Task 11.2 テスト: SyncOrchestrator 自動Confirm統合
# ---------------------------------------------------------------------------

class FakeAuthentikGroupClient:
    def __init__(self, responses):
        self._responses = responses

    def get_group_members(self, group_name):
        from sync import GroupMembersResult
        emails = self._responses.get(group_name, [])
        return GroupMembersResult(group_name, emails, None)


class FakeVaultwardenOrgClientWithConfirm:
    def __init__(self, orgs, states):
        self._orgs = orgs
        self._states = states
        self.invites = []
        self.puts = []
        self.confirms = []

    def authenticate(self):
        return "fake-token"

    def list_organizations(self):
        from sync import Organization
        return [Organization(oid, name) for name, oid in self._orgs.items()]

    def get_org_state(self, org_id):
        for name, oid in self._orgs.items():
            if oid == org_id:
                return self._states[name]
        raise RuntimeError(f"org {org_id} not found")

    def invite_member(self, org_id, email, member_type, collections):
        self.invites.append((org_id, email))

    def put_member_collections(self, org_id, member_id, member_type, collections):
        self.puts.append((org_id, member_id, collections))

    def confirm_member(self, org_id, member_id, user_id, org_key_bytes):
        self.confirms.append((org_id, member_id, user_id))


class FakeDiscordNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, msg):
        self.messages.append(msg)


class TestAutoConfirmInOrchestrator:
    """SyncOrchestrator が org_key_bytes 提供時に Accepted メンバーを自動Confirm することを確認する。"""

    def _make_state_with_accepted(self):
        from sync import OrgState, Member, Collection
        members = [
            Member("m-accepted", "accepted@example.com", 2, 1, [], user_id="uid-accepted"),
        ]
        collections = [Collection("coll-1", "")]
        return OrgState(members=members, collections=collections)

    def test_auto_confirm_runs_for_accepted_members(self):
        """org_key_bytes 指定時、status=1 メンバーを confirm_member で処理する。"""
        from sync import SyncOrchestrator, MappingEntry

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClientWithConfirm(
            orgs={"TestOrg": "org-1"},
            states={"TestOrg": self._make_state_with_accepted()},
        )
        fake_ak = FakeAuthentikGroupClient({"部門A": ["accepted@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
            org_key_bytes=b"\xab" * 64,
        )
        orch.run(dry_run=False)

        assert len(fake_vw.confirms) == 1
        assert fake_vw.confirms[0] == ("org-1", "m-accepted", "uid-accepted")

    def test_auto_confirm_skipped_without_org_key(self):
        """org_key_bytes=None 時は confirm_member を呼ばない。"""
        from sync import SyncOrchestrator, MappingEntry

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClientWithConfirm(
            orgs={"TestOrg": "org-1"},
            states={"TestOrg": self._make_state_with_accepted()},
        )
        fake_ak = FakeAuthentikGroupClient({"部門A": ["accepted@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
            org_key_bytes=None,
        )
        orch.run(dry_run=False)

        assert len(fake_vw.confirms) == 0

    def test_auto_confirm_skipped_in_dry_run(self):
        """dry_run=True 時は confirm_member を呼ばない。"""
        from sync import SyncOrchestrator, MappingEntry

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClientWithConfirm(
            orgs={"TestOrg": "org-1"},
            states={"TestOrg": self._make_state_with_accepted()},
        )
        fake_ak = FakeAuthentikGroupClient({"部門A": ["accepted@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
            org_key_bytes=b"\xab" * 64,
        )
        orch.run(dry_run=True)

        assert len(fake_vw.confirms) == 0

    def test_auto_confirm_error_continues_other_ops(self):
        """confirm_member 失敗時、他の処理は継続する。"""
        from sync import SyncOrchestrator, MappingEntry, VaultwardenApiError

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]

        class FailingConfirmClient(FakeVaultwardenOrgClientWithConfirm):
            def confirm_member(self, org_id, member_id, user_id, org_key_bytes):
                raise VaultwardenApiError("confirm failed")

        fake_vw = FailingConfirmClient(
            orgs={"TestOrg": "org-1"},
            states={"TestOrg": self._make_state_with_accepted()},
        )
        fake_ak = FakeAuthentikGroupClient({"部門A": ["accepted@example.com", "new@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
            org_key_bytes=b"\xab" * 64,
        )
        plan = orch.run(dry_run=False)

        # 招待は継続する (confirm失敗でも処理継続)
        assert len(fake_vw.invites) == 1
        assert fake_vw.invites[0][1] == "new@example.com"
        assert hasattr(plan, "errors")
        assert any("confirm" in e for e in plan.errors)

    def test_discord_summary_shows_auto_confirm_count(self):
        """Discord通知に自動Confirm件数と未Accept件数が含まれる。"""
        from sync import SyncOrchestrator, MappingEntry, OrgState, Member, Collection

        mappings = [MappingEntry("部門A", "TestOrg", "coll-1", "can_view")]

        state = OrgState(
            members=[
                Member("m-accepted", "accepted@example.com", 2, 1, [], user_id="uid-1"),
                Member("m-invited", "invited@example.com", 2, 0, [], user_id="uid-2"),
            ],
            collections=[Collection("coll-1", "")],
        )
        fake_vw = FakeVaultwardenOrgClientWithConfirm(
            orgs={"TestOrg": "org-1"},
            states={"TestOrg": state},
        )
        fake_ak = FakeAuthentikGroupClient(
            {"部門A": ["accepted@example.com", "invited@example.com"]}
        )
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
            org_key_bytes=b"\xab" * 64,
        )
        orch.run(dry_run=False)

        assert len(fake_discord.messages) == 1
        msg = fake_discord.messages[0]
        # 自動Confirm件数が通知に含まれる
        assert "1" in msg  # auto_confirm 1件
        # 未Accept件数が通知に含まれる
        assert "invited" in msg.lower() or "Accept" in msg or "accept" in msg.lower()


# ---------------------------------------------------------------------------
# Task 11.2 テスト: _rsa_oaep_sha1_encrypt (openssl統合テスト)
# ---------------------------------------------------------------------------

def _openssl_available() -> bool:
    """openssl バイナリが利用可能かどうかを確認する。"""
    try:
        result = subprocess.run(["openssl", "version"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _generate_test_rsa_key_pair_der() -> tuple[bytes, bytes]:
    """テスト用RSA-2048鍵ペアを生成し (priv_pem, pub_der) を返す。"""
    priv_result = subprocess.run(
        ["openssl", "genrsa", "2048"], capture_output=True, check=True
    )
    priv_pem = priv_result.stdout

    pub_der_result = subprocess.run(
        ["openssl", "pkey", "-pubout", "-outform", "DER"],
        input=priv_pem, capture_output=True, check=True
    )
    return priv_pem, pub_der_result.stdout


def _generate_test_rsa_pub_key_der_b64() -> str:
    """テスト用RSA-2048公開鍵のbase64エンコードDERを返す (openssl不要の場合はダミー)。"""
    if not _openssl_available():
        return base64.b64encode(b"\x30\x82\x01\x22" + b"\x00" * 290).decode()
    _, pub_der = _generate_test_rsa_key_pair_der()
    return base64.b64encode(pub_der).decode()


import pytest


@pytest.mark.skipif(not _openssl_available(), reason="openssl not available")
class TestRsaOaepSha1Encrypt:
    """_rsa_oaep_sha1_encrypt のopenssl統合テスト。"""

    def test_encrypt_and_decrypt_round_trip(self):
        """暗号化した値を秘密鍵で復号し、元のデータと一致することを確認する。"""
        from sync import _rsa_oaep_sha1_encrypt

        priv_pem, pub_der = _generate_test_rsa_key_pair_der()
        pub_der_b64 = base64.b64encode(pub_der).decode()
        plaintext = b"\xab\xcd\xef" * 21 + b"\x01"  # 64バイト

        encrypted_b64 = _rsa_oaep_sha1_encrypt(pub_der_b64, plaintext)

        # 復号して元データと一致することを確認
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            priv_path = os.path.join(tmpdir, "priv.pem")
            enc_path = os.path.join(tmpdir, "enc.bin")

            with open(priv_path, "wb") as f:
                f.write(priv_pem)
            with open(enc_path, "wb") as f:
                f.write(base64.b64decode(encrypted_b64))

            dec_result = subprocess.run(
                ["openssl", "pkeyutl", "-decrypt",
                 "-inkey", priv_path,
                 "-pkeyopt", "rsa_padding_mode:oaep",
                 "-pkeyopt", "rsa_oaep_md:sha1",
                 "-pkeyopt", "rsa_mgf1_md:sha1",
                 "-in", enc_path],
                capture_output=True, check=True
            )

        assert dec_result.stdout == plaintext

    def test_encrypt_returns_base64_string(self):
        """暗号化結果はbase64文字列である。"""
        from sync import _rsa_oaep_sha1_encrypt

        _, pub_der = _generate_test_rsa_key_pair_der()
        result = _rsa_oaep_sha1_encrypt(base64.b64encode(pub_der).decode(), b"\x00" * 64)

        # base64として有効なことを確認
        decoded = base64.b64decode(result)
        assert len(decoded) == 256  # RSA-2048 = 256バイト出力
