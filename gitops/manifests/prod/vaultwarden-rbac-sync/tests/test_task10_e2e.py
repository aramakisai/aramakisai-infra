"""Task 10.1 / 10.2: オンボーディング・オフボーディング E2E テスト。

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task10_e2e.py -v

10.1 Requirements: 1.1, 6.1, 6.2, 6.4, 6.5, 7.1, 11.4
10.2 Requirements: 8.1, 8.2, 8.3
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import (
    Collection,
    CollectionGrant,
    MappingEntry,
    Member,
    OrgState,
    SyncOrchestrator,
    VAULTWARDEN_STATUS_CONFIRMED,
    VAULTWARDEN_STATUS_ACCEPTED,
    permission_to_collection_grant,
)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------

class FakeAuthentikGroupClient:
    def __init__(self, responses: dict[str, list[str]]):
        self._responses = responses

    def get_group_members(self, group_name: str):
        from sync import GroupMembersResult
        emails = self._responses.get(group_name, [])
        if emails is None:
            return GroupMembersResult(group_name, [], f"group {group_name} not found")
        return GroupMembersResult(group_name, emails, None)


class FakeVaultwardenOrgClient:
    """招待・PUT・Confirmの呼び出しを記録するフェイク。"""

    def __init__(self, orgs: dict, states: dict):
        self._orgs = orgs
        self._states = states
        self.invites: list = []
        self.puts: list = []
        self.confirms: list = []

    def authenticate(self):
        return "fake-token"

    def list_organizations(self):
        from sync import Organization
        return [Organization(oid, name) for name, oid in self._orgs.items()]

    def get_org_state(self, org_id: str):
        for name, oid in self._orgs.items():
            if oid == org_id:
                return self._states[name]
        raise RuntimeError(f"org {org_id} not found")

    def invite_member(self, org_id, email, member_type, collections):
        self.invites.append((org_id, email, collections))

    def put_member_collections(self, org_id, member_id, member_type, collections):
        self.puts.append((org_id, member_id, collections))

    def confirm_member(self, org_id, member_id, user_id, org_key_bytes):
        self.confirms.append((org_id, member_id, user_id))


class FakeDiscordNotifier:
    def __init__(self):
        self.messages: list[str] = []

    def notify(self, message: str):
        self.messages.append(message)


# ---------------------------------------------------------------------------
# 10.1 オンボーディング E2E — 一段階目: 新規メンバー招待フロー
# ---------------------------------------------------------------------------

class TestOnboardingPhase1:
    """新規メンバーがAuthentikグループに追加された際、Vaultwarden招待が送信されることを確認する。

    前提: Vaultwardenには当該メンバーのエントリが存在しない。
    """

    ORG_NAME = "荒牧祭実行委員会"
    ORG_ID = "org-uuid-test"
    COLLECTION_ID = "coll-sns-uuid"
    ALICE = "alice@example.com"

    def _make_orchestrator(self, vw_members: list[Member]):
        """マッピング1件・メンバー1名のシンプルなオーケストレーター。"""
        mapping = MappingEntry("広報", self.ORG_NAME, self.COLLECTION_ID, "can_view_except_passwords")
        org_state = OrgState(
            members=vw_members,
            collections=[Collection(self.COLLECTION_ID, "encrypted-name")],
        )
        fake_vw = FakeVaultwardenOrgClient(
            orgs={self.ORG_NAME: self.ORG_ID},
            states={self.ORG_NAME: org_state},
        )
        fake_ak = FakeAuthentikGroupClient({"広報": [self.ALICE]})
        fake_discord = FakeDiscordNotifier()
        orch = SyncOrchestrator(
            mappings=[mapping],
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        return orch, fake_vw, fake_discord

    def test_invite_sent_when_new_member_not_in_vaultwarden(self):
        """Vaultwarden未登録のメンバーに招待が送信される (Requirement 6.1)。"""
        orch, fake_vw, _ = self._make_orchestrator(vw_members=[])

        plan = orch.run(dry_run=False)

        assert len(plan.invites) == 1, "新規メンバーへの招待が1件発生する"
        assert plan.invites[0].email == self.ALICE
        assert len(fake_vw.invites) == 1, "invite_member が1回呼ばれる"
        assert fake_vw.invites[0][1] == self.ALICE

    def test_invite_includes_correct_collection_grant(self):
        """招待時に正しいCollection権限が指定される (Requirement 6.4)。"""
        orch, fake_vw, _ = self._make_orchestrator(vw_members=[])

        orch.run(dry_run=False)

        _, _, collections = fake_vw.invites[0]
        assert len(collections) == 1
        grant = collections[0]
        assert grant.collection_id == self.COLLECTION_ID
        assert grant.read_only is True       # can_view_except_passwords: readOnly=True
        assert grant.hide_passwords is True  # can_view_except_passwords: hidePasswords=True
        assert grant.manage is False

    def test_discord_shows_invite_count(self):
        """Discordに招待件数サマリーが通知される (Requirement 11.4)。"""
        orch, _, fake_discord = self._make_orchestrator(vw_members=[])

        orch.run(dry_run=False)

        assert len(fake_discord.messages) == 1
        msg = fake_discord.messages[0]
        assert "招待: 1件" in msg

    def test_no_put_on_first_invite(self):
        """初回招待ではCollection権限PUTは不要（招待APIが権限も指定するため）。"""
        orch, fake_vw, _ = self._make_orchestrator(vw_members=[])

        orch.run(dry_run=False)

        assert len(fake_vw.puts) == 0, "招待のみでPUTは呼ばれない"

    def test_dry_run_skips_invite(self):
        """dry-run時はinvite_memberを呼ばない (Requirement 6.5)。"""
        orch, fake_vw, _ = self._make_orchestrator(vw_members=[])

        plan = orch.run(dry_run=True)

        assert len(plan.invites) == 1, "dry-run でも差分は計算される"
        assert len(fake_vw.invites) == 0, "dry-run では invite_member が呼ばれない"


# ---------------------------------------------------------------------------
# 10.1 オンボーディング E2E — 一段階目続き: 招待済み未Accept状態
# ---------------------------------------------------------------------------

class TestOnboardingPhase1Invited:
    """招待送信後、ユーザーがAcceptするまでの同期サイクルを確認する。

    前提: VaultwardenにメンバーはいるがStatus=Invited(0) — Acceptリンク未クリック。
    期待: confirm_pendingに含まれ、Discord通知で「未Accept」として報告される。
    """

    ORG_NAME = "荒牧祭実行委員会"
    ORG_ID = "org-uuid-test"
    COLLECTION_ID = "coll-sns-uuid"
    ALICE = "alice@example.com"

    def _make_orchestrator(self):
        mapping = MappingEntry("広報", self.ORG_NAME, self.COLLECTION_ID, "can_view_except_passwords")
        invited_member = Member(
            member_id="mem-alice-invited",
            email=self.ALICE,
            member_type=2,
            status=0,  # Invited — Acceptリンク未クリック
            collections=[],
            user_id="user-alice-uuid",
        )
        org_state = OrgState(
            members=[invited_member],
            collections=[Collection(self.COLLECTION_ID, "encrypted-name")],
        )
        fake_vw = FakeVaultwardenOrgClient(
            orgs={self.ORG_NAME: self.ORG_ID},
            states={self.ORG_NAME: org_state},
        )
        fake_ak = FakeAuthentikGroupClient({"広報": [self.ALICE]})
        fake_discord = FakeDiscordNotifier()
        orch = SyncOrchestrator(
            mappings=[mapping],
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        return orch, fake_vw, fake_discord

    def test_invited_member_appears_in_confirm_pending(self):
        """status=0メンバーはconfirm_pendingに追加され、新規招待は行われない (Req 6.2)。"""
        orch, fake_vw, _ = self._make_orchestrator()

        plan = orch.run(dry_run=False)

        assert len(plan.invites) == 0, "既に招待済みのメンバーに再度招待しない"
        assert len(fake_vw.invites) == 0
        assert len(plan.confirm_pending) == 1
        assert plan.confirm_pending[0].email == self.ALICE

    def test_discord_reports_accept_pending_members(self):
        """Discordに未Accept件数と対象メールが通知される (Requirement 11.4)。"""
        orch, _, fake_discord = self._make_orchestrator()

        orch.run(dry_run=False)

        assert len(fake_discord.messages) == 1
        msg = fake_discord.messages[0]
        assert "招待済み・未Accept: 1件" in msg
        assert self.ALICE in msg

    def test_no_auto_confirm_for_status0(self):
        """status=0(Invited)はauto_confirmの対象外 (Acceptリンク未クリック)。"""
        orch, fake_vw, _ = self._make_orchestrator()

        plan = orch.run(dry_run=False)

        assert len(plan.auto_confirm) == 0
        assert len(fake_vw.confirms) == 0


# ---------------------------------------------------------------------------
# 10.1 オンボーディング E2E — 二段階目: Accept後の自動Confirm + Collection権限適用
# ---------------------------------------------------------------------------

class TestOnboardingPhase2:
    """ユーザーがAcceptリンクをクリックした後の同期サイクルを確認する。

    前提: VaultwardenにStatus=Accepted(1)のメンバーが存在する。
    期待: 自動Confirmと Collection権限PUTが実行される。
    """

    ORG_NAME = "荒牧祭実行委員会"
    ORG_ID = "org-uuid-test"
    COLLECTION_ID = "coll-sns-uuid"
    ALICE = "alice@example.com"
    ALICE_MEMBER_ID = "mem-alice-accepted"
    ALICE_USER_ID = "user-alice-uuid"

    def _make_orchestrator(self, org_key_bytes: bytes | None = b"\xab" * 64):
        mapping = MappingEntry("広報", self.ORG_NAME, self.COLLECTION_ID, "can_view_except_passwords")
        accepted_member = Member(
            member_id=self.ALICE_MEMBER_ID,
            email=self.ALICE,
            member_type=2,
            status=VAULTWARDEN_STATUS_ACCEPTED,  # status=1: Accept済み、Confirm前
            collections=[],
            user_id=self.ALICE_USER_ID,
        )
        org_state = OrgState(
            members=[accepted_member],
            collections=[Collection(self.COLLECTION_ID, "encrypted-name")],
        )
        fake_vw = FakeVaultwardenOrgClient(
            orgs={self.ORG_NAME: self.ORG_ID},
            states={self.ORG_NAME: org_state},
        )
        fake_ak = FakeAuthentikGroupClient({"広報": [self.ALICE]})
        fake_discord = FakeDiscordNotifier()
        orch = SyncOrchestrator(
            mappings=[mapping],
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
            org_key_bytes=org_key_bytes,
        )
        return orch, fake_vw, fake_discord

    def test_auto_confirm_called_after_member_accepts(self):
        """status=1メンバーにconfirm_memberが呼ばれる (Requirement 6.3 → task 11自動化)。"""
        orch, fake_vw, _ = self._make_orchestrator()

        plan = orch.run(dry_run=False)

        assert len(plan.auto_confirm) == 1
        assert plan.auto_confirm[0].email == self.ALICE
        assert len(fake_vw.confirms) == 1
        assert fake_vw.confirms[0] == (self.ORG_ID, self.ALICE_MEMBER_ID, self.ALICE_USER_ID)

    def test_collection_permissions_applied_for_accepted_member(self):
        """auto_confirm後にCollection権限PUTが実行される (Requirement 7.1)。"""
        orch, fake_vw, _ = self._make_orchestrator()

        orch.run(dry_run=False)

        assert len(fake_vw.puts) == 1, "Collection権限PUTが1回呼ばれる"
        org_id, member_id, collections = fake_vw.puts[0]
        assert org_id == self.ORG_ID
        assert member_id == self.ALICE_MEMBER_ID
        assert len(collections) == 1
        assert collections[0].collection_id == self.COLLECTION_ID

    def test_no_invite_for_already_registered_member(self):
        """Accepted状態のメンバーに再度招待を送らない (Requirement 6.1)。"""
        orch, fake_vw, _ = self._make_orchestrator()

        plan = orch.run(dry_run=False)

        assert len(plan.invites) == 0
        assert len(fake_vw.invites) == 0

    def test_accepted_member_not_in_confirm_pending(self):
        """status=1メンバーはconfirm_pendingに含まれない (task 11分離)。"""
        orch, _, _ = self._make_orchestrator()

        plan = orch.run(dry_run=False)

        assert len(plan.confirm_pending) == 0

    def test_discord_shows_auto_confirm_count(self):
        """Discordに自動Confirm件数が通知される (Requirement 11.4)。"""
        orch, _, fake_discord = self._make_orchestrator()

        orch.run(dry_run=False)

        msg = fake_discord.messages[0]
        assert "自動Confirm: 1件" in msg
        assert self.ALICE in msg

    def test_auto_confirm_skipped_without_org_key(self):
        """VAULTWARDEN_ORG_KEY未設定時はauto_confirmをスキップ (後方互換)。"""
        orch, fake_vw, _ = self._make_orchestrator(org_key_bytes=None)

        plan = orch.run(dry_run=False)

        assert len(fake_vw.confirms) == 0
        assert len(plan.auto_confirm) == 1  # planには記録される (dry_runと同様)


# ---------------------------------------------------------------------------
# 10.2 オフボーディング E2E — グループ脱退後のCollection権限剥奪
# ---------------------------------------------------------------------------

class TestOffboardingE2E:
    """メンバーがAuthentikグループから脱退した後、Vaultwardenの
    Collection権限が剥奪されることを確認する (Requirement 8.1, 8.2, 8.3)。

    Organizationからの除名は行わない (design.md Non-Goal)。
    """

    ORG_NAME = "荒牧祭実行委員会"
    ORG_ID = "org-uuid-test"
    COLLECTION_ID = "coll-sns-uuid"
    BOB = "bob@example.com"
    BOB_MEMBER_ID = "mem-bob"

    def _make_orchestrator(self, ak_members: list[str]):
        """ak_members: 広報グループの現在のメンバーリスト。"""
        mapping = MappingEntry("広報", self.ORG_NAME, self.COLLECTION_ID, "can_view_except_passwords")
        grant = permission_to_collection_grant(self.COLLECTION_ID, "can_view_except_passwords")
        bob_member = Member(
            member_id=self.BOB_MEMBER_ID,
            email=self.BOB,
            member_type=2,
            status=VAULTWARDEN_STATUS_CONFIRMED,
            collections=[grant],
        )
        org_state = OrgState(
            members=[bob_member],
            collections=[Collection(self.COLLECTION_ID, "encrypted-name")],
        )
        fake_vw = FakeVaultwardenOrgClient(
            orgs={self.ORG_NAME: self.ORG_ID},
            states={self.ORG_NAME: org_state},
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ak_members})
        fake_discord = FakeDiscordNotifier()
        orch = SyncOrchestrator(
            mappings=[mapping],
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        return orch, fake_vw, fake_discord

    def test_collection_permission_revoked_when_member_leaves_group(self):
        """グループ脱退メンバーのCollection権限が剥奪される (Requirement 8.1)。"""
        orch, fake_vw, _ = self._make_orchestrator(ak_members=[])  # bob not in group

        plan = orch.run(dry_run=False)

        assert len(plan.collection_removals) == 1, "1件の権限削除が発生する"
        assert plan.collection_removals[0].member_id == self.BOB_MEMBER_ID
        assert self.COLLECTION_ID in plan.collection_removals[0].collection_ids

    def test_put_called_with_empty_collections_on_offboarding(self):
        """権限削除はPUT(空コレクション)で実施される — メンバーを除名しない (Req 8.3)。"""
        orch, fake_vw, _ = self._make_orchestrator(ak_members=[])

        orch.run(dry_run=False)

        assert len(fake_vw.puts) == 1, "put_member_collectionsが1回呼ばれる"
        org_id, member_id, collections = fake_vw.puts[0]
        assert org_id == self.ORG_ID
        assert member_id == self.BOB_MEMBER_ID
        assert collections == [], "全Collection権限が空になる"

    def test_member_stays_in_org_collection_only_revoked(self):
        """Organizationからの除名は発生しない — Collection権限のみ剥奪 (Requirement 8.2)。"""
        orch, fake_vw, _ = self._make_orchestrator(ak_members=[])

        plan = orch.run(dry_run=False)

        # invite (追加) も呼ばれない、削除APIも存在しない
        assert len(fake_vw.invites) == 0, "除名も再招待も発生しない"
        assert len(fake_vw.confirms) == 0

        # plan.collection_removals の member_id で元のメンバーIDが保持されていることを確認
        assert plan.collection_removals[0].member_id == self.BOB_MEMBER_ID

    def test_discord_shows_collection_removal_count(self):
        """Discordに権限削除件数が通知される (Requirement 11.4)。"""
        orch, _, fake_discord = self._make_orchestrator(ak_members=[])

        orch.run(dry_run=False)

        assert len(fake_discord.messages) == 1
        msg = fake_discord.messages[0]
        assert "権限削除: 1件" in msg

    def test_active_member_unchanged_when_still_in_group(self):
        """グループ継続所属メンバーの権限は変更されない (冪等性 / Requirement 8.2)。"""
        orch, fake_vw, _ = self._make_orchestrator(ak_members=[self.BOB])  # bob still in group

        plan = orch.run(dry_run=False)

        assert len(plan.collection_removals) == 0, "脱退していないメンバーの削除は発生しない"
        assert len(fake_vw.puts) == 0, "権限が既に一致しているためPUTは呼ばれない"
        assert plan.unchanged_count == 1

    def test_dry_run_skips_put_on_offboarding(self):
        """dry-run時はPUTを呼ばない (Requirement 8.1)。"""
        orch, fake_vw, _ = self._make_orchestrator(ak_members=[])

        plan = orch.run(dry_run=True)

        assert len(plan.collection_removals) == 1, "dry-runでも差分は計算される"
        assert len(fake_vw.puts) == 0, "dry-runではPUTが呼ばれない"


# ---------------------------------------------------------------------------
# 10.1/10.2 複数メンバー混在シナリオ
# ---------------------------------------------------------------------------

class TestMixedMemberScenario:
    """オンボーディング中のメンバーとオフボーディング対象メンバーが
    同一Organizationに混在する場合の正確な差分計算を確認する。
    """

    ORG_NAME = "荒牧祭実行委員会"
    ORG_ID = "org-uuid-test"
    COLLECTION_ID = "coll-sns-uuid"

    def test_invite_and_revoke_computed_independently(self):
        """新規メンバー招待と脱退メンバー権限削除が独立して処理される。"""
        from sync import MappingEntry, OrgState, Member, Collection

        mapping = MappingEntry("広報", self.ORG_NAME, self.COLLECTION_ID, "can_view_except_passwords")
        grant = permission_to_collection_grant(self.COLLECTION_ID, "can_view_except_passwords")

        # VaultwardenにはBobが存在 (脱退予定) でAliceは不在 (新規参加予定)
        bob = Member("mem-bob", "bob@example.com", 2, VAULTWARDEN_STATUS_CONFIRMED, [grant])
        org_state = OrgState(
            members=[bob],
            collections=[Collection(self.COLLECTION_ID, "encrypted-name")],
        )
        fake_vw = FakeVaultwardenOrgClient(
            orgs={self.ORG_NAME: self.ORG_ID},
            states={self.ORG_NAME: org_state},
        )
        # AuthentikグループにはAliceがいてBobは脱退済み
        fake_ak = FakeAuthentikGroupClient({"広報": ["alice@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=[mapping],
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=False)

        assert len(plan.invites) == 1, "Aliceへの招待が発生する"
        assert plan.invites[0].email == "alice@example.com"
        assert len(plan.collection_removals) == 1, "Bobの権限削除が発生する"
        assert plan.collection_removals[0].member_id == "mem-bob"

        # invite と put (removal) がそれぞれ1回ずつ呼ばれる
        assert len(fake_vw.invites) == 1
        assert len(fake_vw.puts) == 1  # Bobの権限削除PUT
