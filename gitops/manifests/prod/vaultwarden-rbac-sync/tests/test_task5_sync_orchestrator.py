"""Task 5: SyncOrchestrator TDD tests.

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task5_sync_orchestrator.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import (
    CollectionGrant,
    MappingEntry,
    Member,
    OrgState,
    SyncPlan,
    InvitePlan,
    UpdatePlan,
    RemovalPlan,
    ConfirmPendingPlan,
    permission_to_collection_grant,
)


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
    def __init__(self, orgs: dict, states: dict):
        self._orgs = orgs  # name -> id
        self._states = states  # name -> OrgState
        self.invites = []
        self.puts = []

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
        self.invites.append((org_id, email, member_type, collections))

    def put_member_collections(self, org_id, member_id, member_type, collections):
        self.puts.append((org_id, member_id, member_type, collections))


class FakeDiscordNotifier:
    def __init__(self):
        self.messages = []

    def notify(self, message: str):
        self.messages.append(message)


class TestSyncOrchestrator:
    """Requirement 5.3, 6.1, 6.3, 6.4, 6.5, 7.1, 8.1, 9.1, 9.2"""

    def _make_state(self, members, collection_ids=None):
        collections = [CollectionGrant(cid, False, False, False) for cid in (collection_ids or [])]
        return OrgState(members=members, collections=collections)

    # -- dry-run mode -------------------------------------------------------

    def test_dry_run_no_api_changes(self):
        """dry_run=True → no Vaultwarden changes (Req 9.1)."""
        from sync import SyncOrchestrator

        mappings = [MappingEntry("広報", "SNS", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClient(
            orgs={"SNS": "org-1"},
            states={
                "SNS": self._make_state(
                    members=[Member("m1", "old@example.com", 2, 2, [])],
                    collection_ids=["coll-1"],
                )
            },
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ["new@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=True)

        assert len(fake_vw.invites) == 0
        assert len(fake_vw.puts) == 0
        assert isinstance(plan, SyncPlan)
        assert len(plan.invites) == 1

    def test_dry_run_still_computes_diff(self):
        """dry_run=True → still fetches data and computes diff (Req 9.2)."""
        from sync import SyncOrchestrator

        mappings = [MappingEntry("広報", "SNS", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClient(
            orgs={"SNS": "org-1"},
            states={
                "SNS": self._make_state(
                    members=[Member("m1", "u@example.com", 2, 2, [])],
                    collection_ids=["coll-1"],
                )
            },
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ["u@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=True)

        assert len(plan.collection_updates) == 1  # member needs collection added
        assert plan.unchanged_count == 0

    # -- normal execution ---------------------------------------------------

    def test_normal_run_invites_and_updates(self):
        """Normal run applies invites and updates (Req 6.1, 7.1)."""
        from sync import SyncOrchestrator

        mappings = [MappingEntry("広報", "SNS", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClient(
            orgs={"SNS": "org-1"},
            states={
                "SNS": self._make_state(
                    members=[Member("m1", "old@example.com", 2, 2, [])],
                    collection_ids=["coll-1"],
                )
            },
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ["new@example.com", "old@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=False)

        assert len(fake_vw.invites) == 1
        assert fake_vw.invites[0][1] == "new@example.com"
        # old@example.com already member, no update needed (no collections yet)
        assert len(fake_vw.puts) == 1  # update for old@example.com with collection

    def test_individual_error_continues(self):
        """Individual invite failure is recorded, others continue (Req 6.3)."""
        from sync import SyncOrchestrator, VaultwardenApiError

        mappings = [MappingEntry("広報", "SNS", "coll-1", "can_view")]

        class FailingVW(FakeVaultwardenOrgClient):
            def invite_member(self, org_id, email, member_type, collections):
                if email == "fail@example.com":
                    raise VaultwardenApiError("invite failed")
                super().invite_member(org_id, email, member_type, collections)

        fake_vw = FailingVW(
            orgs={"SNS": "org-1"},
            states={
                "SNS": self._make_state(
                    members=[Member("m1", "old@example.com", 2, 2, [])],
                    collection_ids=["coll-1"],
                )
            },
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ["fail@example.com", "ok@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=False)

        assert len(fake_vw.invites) == 1  # only ok@example.com succeeded
        assert fake_vw.invites[0][1] == "ok@example.com"
        assert len(plan.errors) == 1
        assert "fail@example.com" in plan.errors[0]

    # -- confirm pending detection ------------------------------------------

    def test_confirm_pending_included_in_plan(self):
        """Invited but unconfirmed members appear in plan (Req 6.5)."""
        from sync import SyncOrchestrator

        mappings = [MappingEntry("広報", "SNS", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClient(
            orgs={"SNS": "org-1"},
            states={
                "SNS": self._make_state(
                    members=[Member("m1", "pending@example.com", 2, 0, [])],  # status 0 = invited
                    collection_ids=["coll-1"],
                )
            },
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ["pending@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=False)

        assert len(plan.confirm_pending) == 1
        assert plan.confirm_pending[0].email == "pending@example.com"

    # -- discord notification -----------------------------------------------

    def test_discord_not_notified_without_confirm_pending(self):
        """confirm_pendingがない場合はDiscordに通知しない。"""
        from sync import SyncOrchestrator

        mappings = [MappingEntry("広報", "SNS", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClient(
            orgs={"SNS": "org-1"},
            states={
                "SNS": self._make_state(
                    members=[Member("m1", "u@example.com", 2, 2, [])],
                    collection_ids=["coll-1"],
                )
            },
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ["u@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        orch.run(dry_run=False)

        assert len(fake_discord.messages) == 0

    def test_discord_notified_for_new_confirm_pending(self):
        """新規のconfirm_pendingメンバーがいる場合はDiscordに通知する。"""
        from sync import SyncOrchestrator

        mappings = [MappingEntry("広報", "SNS", "coll-1", "can_view")]
        fake_vw = FakeVaultwardenOrgClient(
            orgs={"SNS": "org-1"},
            states={
                "SNS": self._make_state(
                    members=[Member("m1", "pending@example.com", 2, 0, [])],
                    collection_ids=["coll-1"],
                )
            },
        )
        fake_ak = FakeAuthentikGroupClient({"広報": ["pending@example.com"]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        orch.run(dry_run=False)

        assert len(fake_discord.messages) == 1
        assert "pending@example.com" in fake_discord.messages[0]
