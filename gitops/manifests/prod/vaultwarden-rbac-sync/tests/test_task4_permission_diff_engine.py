"""Task 4: PermissionDiffEngine TDD tests.

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task4_permission_diff_engine.py -v
"""
import sys
from pathlib import Path

# Add parent directory to import sync.py as module
sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import (
    CollectionGrant,
    MappingEntry,
    Member,
    OrgState,
    Organization,
    filter_confirm_pending,
    merge_collection_grants,
    permission_to_collection_grant,
    remove_collection_grants,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_org_state(members: list[Member], collection_ids: list[str] | None = None) -> OrgState:
    collections = [CollectionGrant(cid, False, False, False) for cid in (collection_ids or [])]
    return OrgState(members=members, collections=collections)


# ---------------------------------------------------------------------------
# RED tests — will fail until PermissionDiffEngine is implemented
# ---------------------------------------------------------------------------

class TestPermissionDiffEngine:
    """Requirement 5.1, 5.2, 5.3, 8.1, 8.2, 8.3"""

    # -- invite detection ---------------------------------------------------

    def test_invite_new_member_not_in_org(self):
        """Authentik group member not in Vaultwarden org → invite target."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["new@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[Member("m1", "old@example.com", 2, 2, [])],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.invites) == 1
        assert plan.invites[0].email == "new@example.com"
        assert plan.invites[0].org_id == "org-1"
        assert plan.invites[0].collections == [permission_to_collection_grant("coll-1", "can_view")]

    def test_no_invite_for_existing_member(self):
        """Existing member already in org → not in invites."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["exists@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[Member("m1", "exists@example.com", 2, 2, [])],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.invites) == 0

    # -- collection update detection ----------------------------------------

    def test_update_wrong_permission(self):
        """Member has collection but wrong permission → update target."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_manage"),
        ]
        group_members = {"広報": ["user@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member(
                        "m1",
                        "user@example.com",
                        2,
                        2,
                        [CollectionGrant("coll-1", True, False, False)],  # can_view currently
                    )
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.collection_updates) == 1
        upd = plan.collection_updates[0]
        assert upd.member_id == "m1"
        assert upd.org_id == "org-1"
        assert upd.member_type == 2
        assert CollectionGrant("coll-1", False, False, True) in upd.collections

    def test_no_update_when_permission_matches(self):
        """Member has exact permission → excluded from updates (Req 5.2)."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["user@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member(
                        "m1",
                        "user@example.com",
                        2,
                        2,
                        [CollectionGrant("coll-1", True, False, False)],
                    )
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.collection_updates) == 0
        assert plan.unchanged_count == 1

    # -- collection removal detection ---------------------------------------

    def test_remove_collection_when_left_group(self):
        """Member left group → collection removal target (Req 8.1)."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": []}  # nobody left
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member(
                        "m1",
                        "gone@example.com",
                        2,
                        2,
                        [CollectionGrant("coll-1", True, False, False)],
                    )
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.collection_removals) == 1
        rem = plan.collection_removals[0]
        assert rem.member_id == "m1"
        assert rem.org_id == "org-1"
        assert rem.member_type == 2
        assert rem.collection_ids == {"coll-1"}

    def test_no_removal_when_still_in_group(self):
        """Member still in group → no removal (Req 8.3 sanity)."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["user@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member(
                        "m1",
                        "user@example.com",
                        2,
                        2,
                        [CollectionGrant("coll-1", True, False, False)],
                    )
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.collection_removals) == 0

    # -- multi-group partial leave (Req 8.2) --------------------------------

    def test_partial_leave_keeps_other_group_collection(self):
        """User leaves one of multiple groups → only that group's collection removed."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
            MappingEntry("総務", "SNSアカウント", "coll-2", "can_edit"),
        ]
        # user left 広報 but still in 総務
        group_members = {"広報": [], "総務": ["user@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member(
                        "m1",
                        "user@example.com",
                        2,
                        2,
                        [
                            CollectionGrant("coll-1", True, False, False),
                            CollectionGrant("coll-2", False, False, False),
                        ],
                    )
                ],
                collection_ids=["coll-1", "coll-2"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        # Partial leave: full-replace PUT with remaining collections removes coll-1
        assert len(plan.collection_removals) == 0
        assert len(plan.collection_updates) == 1
        upd = plan.collection_updates[0]
        assert CollectionGrant("coll-2", False, False, False) in upd.collections
        # coll-1 should not be in the update (it gets removed by full-replace)
        assert not any(g.collection_id == "coll-1" for g in upd.collections)

    # -- aggregation per user×org (Req 7.2) ---------------------------------

    def test_aggregate_multiple_collection_changes_per_member(self):
        """Same user has changes in multiple collections → single update plan."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
            MappingEntry("総務", "SNSアカウント", "coll-2", "can_manage"),
        ]
        group_members = {
            "広報": ["user@example.com"],
            "総務": ["user@example.com"],
        }
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member(
                        "m1",
                        "user@example.com",
                        2,
                        2,
                        [],  # no collections yet
                    )
                ],
                collection_ids=["coll-1", "coll-2"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.collection_updates) == 1
        upd = plan.collection_updates[0]
        assert len(upd.collections) == 2
        assert CollectionGrant("coll-1", True, False, False) in upd.collections
        assert CollectionGrant("coll-2", False, False, True) in upd.collections

    # -- confirm pending detection (Req 6.5) --------------------------------

    def test_confirm_pending_detected(self):
        """Invited (status != Confirmed) members appear in confirm_pending list."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["pending@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member("m1", "pending@example.com", 2, 0, [])  # status 0 = invited
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.confirm_pending) == 1
        assert plan.confirm_pending[0].email == "pending@example.com"
        assert plan.confirm_pending[0].org_id == "org-1"

    def test_confirmed_member_not_in_confirm_pending(self):
        """Confirmed members should not appear in confirm_pending."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["ok@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member("m1", "ok@example.com", 2, 2, [CollectionGrant("coll-1", True, False, False)])
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.confirm_pending) == 0

    # -- missing group handling ---------------------------------------------

    def test_missing_group_skipped_with_error(self):
        """Group not found in Authentik → no members, mapping treated as no one."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("存在しない", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {}  # group missing
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[Member("m1", "user@example.com", 2, 2, [CollectionGrant("coll-1", True, False, False)])],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        # No members in group → collection should be removed from existing user
        assert len(plan.collection_removals) == 1

    # -- unchanged count ----------------------------------------------------

    def test_unchanged_count_excludes_changes(self):
        """Only members with no changes count as unchanged."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["same@example.com", "new@example.com"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member("m1", "same@example.com", 2, 2, [CollectionGrant("coll-1", True, False, False)]),
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert plan.unchanged_count == 1  # same@example.com only
        assert len(plan.invites) == 1      # new@example.com

    # -- case insensitive email comparison ----------------------------------

    def test_email_case_insensitive(self):
        """Email comparison should be case-insensitive to avoid duplicate invites."""
        from sync import PermissionDiffEngine

        mappings = [
            MappingEntry("広報", "SNSアカウント", "coll-1", "can_view"),
        ]
        group_members = {"広報": ["UPPER@EXAMPLE.COM"]}
        org_states = {
            "SNSアカウント": _make_org_state(
                members=[
                    Member("m1", "upper@example.com", 2, 2, [CollectionGrant("coll-1", True, False, False)])
                ],
                collection_ids=["coll-1"],
            )
        }
        org_ids = {"SNSアカウント": "org-1"}

        plan = PermissionDiffEngine.compute_diff(mappings, group_members, org_states, org_ids)

        assert len(plan.invites) == 0
        assert len(plan.collection_updates) == 0
        assert plan.unchanged_count == 1
