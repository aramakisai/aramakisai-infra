"""Task 10.3: 冪等性・同時実行排他のリグレッション確認。

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task10_regression.py -v

Requirement 10.2, 10.3, 13.3, 13.4
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import (
    CollectionGrant,
    Collection,
    MappingEntry,
    Member,
    OrgState,
    SyncLockManager,
    SyncOrchestrator,
    VAULTWARDEN_STATUS_CONFIRMED,
    permission_to_collection_grant,
    run_cron_mode,
)


# ---------------------------------------------------------------------------
# Shared fakes (reuse patterns from test_task5 / test_task7)
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
    def __init__(self, orgs: dict, states: dict):
        self._orgs = orgs
        self._states = states
        self.invites: list = []
        self.puts: list = []

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
        self.messages: list[str] = []

    def notify(self, message: str):
        self.messages.append(message)


class FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeLeaseStore:
    """kubectl create/get/replace/delete の実API意味論を模倣 (task 7 と同じフェイク)。"""

    def __init__(self):
        self.lease: dict | None = None
        self._resource_version = 0

    def run(self, cmd, **kwargs):
        if cmd[:2] == ["kubectl", "create"]:
            return self._create(json.loads(kwargs["input"]))
        if cmd[:2] == ["kubectl", "get"]:
            return self._get()
        if cmd[:2] == ["kubectl", "replace"]:
            return self._replace(json.loads(kwargs["input"]))
        if cmd[:2] == ["kubectl", "delete"]:
            self.lease = None
            return FakeCompletedProcess(returncode=0)
        raise AssertionError(f"unexpected kubectl invocation: {cmd}")

    def _create(self, manifest):
        if self.lease is not None:
            return FakeCompletedProcess(
                returncode=1,
                stderr='Error from server (AlreadyExists): leases.coordination.k8s.io '
                       '"vaultwarden-rbac-sync-lock" already exists',
            )
        self._resource_version += 1
        manifest["metadata"]["resourceVersion"] = str(self._resource_version)
        self.lease = manifest
        return FakeCompletedProcess(returncode=0)

    def _get(self):
        if self.lease is None:
            return FakeCompletedProcess(returncode=1, stderr="NotFound")
        return FakeCompletedProcess(returncode=0, stdout=json.dumps(self.lease))

    def _replace(self, manifest):
        if self.lease is None:
            return FakeCompletedProcess(returncode=1, stderr="NotFound")
        if manifest["metadata"].get("resourceVersion") != self.lease["metadata"]["resourceVersion"]:
            return FakeCompletedProcess(returncode=1, stderr="Conflict")
        self._resource_version += 1
        manifest["metadata"]["resourceVersion"] = str(self._resource_version)
        self.lease = manifest
        return FakeCompletedProcess(returncode=0)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_org_state_matching_mapping(mapping: MappingEntry, email: str) -> OrgState:
    """AuthentikグループメンバーとVaultwarden現状が完全に一致する状態を生成する。"""
    desired_grant = permission_to_collection_grant(mapping.collection_id, mapping.permission)
    member = Member(
        member_id="m-confirmed",
        email=email,
        member_type=2,
        status=VAULTWARDEN_STATUS_CONFIRMED,
        collections=[desired_grant],
    )
    collection = Collection(mapping.collection_id, "encrypted-name")
    return OrgState(members=[member], collections=[collection])


# ---------------------------------------------------------------------------
# 10.3-A: 冪等性 — 差分なし時にVaultwarden変更ゼロ (Requirement 10.3)
# ---------------------------------------------------------------------------

class TestIdempotency:
    """差分が存在しない状態でCronJobを実行した場合、Vaultwarden側に変更が発生せず正常終了する。"""

    def test_no_mutations_when_state_already_matches(self):
        """AuthentikグループメンバーとVaultwarden権限が完全一致 → invite/putゼロ。"""
        mapping = MappingEntry("広報", "荒牧祭実行委員会", "coll-uuid", "can_view_except_passwords")
        email = "member@example.com"

        fake_vw = FakeVaultwardenOrgClient(
            orgs={"荒牧祭実行委員会": "org-uuid"},
            states={"荒牧祭実行委員会": _make_org_state_matching_mapping(mapping, email)},
        )
        fake_ak = FakeAuthentikGroupClient({"広報": [email]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=[mapping],
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=False)

        assert len(fake_vw.invites) == 0, "diff-less run must not send any invites"
        assert len(fake_vw.puts) == 0, "diff-less run must not call put_member_collections"
        assert plan.unchanged_count == 1
        assert len(plan.invites) == 0
        assert len(plan.collection_updates) == 0
        assert len(plan.collection_removals) == 0

    def test_no_mutations_multi_collection_already_synced(self):
        """複数Collection・複数メンバーが全てマッピングと一致 → 全員がunchangedとして扱われる。"""
        mappings = [
            MappingEntry("広報", "荒牧祭実行委員会", "coll-a", "can_view_except_passwords"),
            MappingEntry("広報", "荒牧祭実行委員会", "coll-b", "can_manage"),
        ]
        email1, email2 = "alice@example.com", "bob@example.com"

        grant_a = permission_to_collection_grant("coll-a", "can_view_except_passwords")
        grant_b = permission_to_collection_grant("coll-b", "can_manage")

        members = [
            Member("m1", email1, 2, VAULTWARDEN_STATUS_CONFIRMED, [grant_a, grant_b]),
            Member("m2", email2, 2, VAULTWARDEN_STATUS_CONFIRMED, [grant_a, grant_b]),
        ]
        org_state = OrgState(
            members=members,
            collections=[Collection("coll-a", "enc"), Collection("coll-b", "enc")],
        )

        fake_vw = FakeVaultwardenOrgClient(
            orgs={"荒牧祭実行委員会": "org-uuid"},
            states={"荒牧祭実行委員会": org_state},
        )
        fake_ak = FakeAuthentikGroupClient({"広報": [email1, email2]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=mappings,
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        plan = orch.run(dry_run=False)

        assert len(fake_vw.invites) == 0
        assert len(fake_vw.puts) == 0
        assert plan.unchanged_count == 2

    def test_discord_not_notified_on_idempotent_run(self):
        """冪等実行（変更なし・confirm_pending=0）ではDiscordに通知しない。"""
        mapping = MappingEntry("総務", "荒牧祭実行委員会", "coll-uuid", "can_edit")
        email = "soumu@example.com"

        fake_vw = FakeVaultwardenOrgClient(
            orgs={"荒牧祭実行委員会": "org-uuid"},
            states={"荒牧祭実行委員会": _make_org_state_matching_mapping(mapping, email)},
        )
        fake_ak = FakeAuthentikGroupClient({"総務": [email]})
        fake_discord = FakeDiscordNotifier()

        orch = SyncOrchestrator(
            mappings=[mapping],
            authentik_client=fake_ak,
            vaultwarden_client=fake_vw,
            discord_notifier=fake_discord,
        )
        orch.run(dry_run=False)

        assert len(fake_discord.messages) == 0


# ---------------------------------------------------------------------------
# 10.3-B: 同時実行排他 — Lease競合で片方のみ実行 (Requirement 10.2, 13.3, 13.4)
# ---------------------------------------------------------------------------

class TestConcurrencyExclusion:
    """CronJobとイベント駆動トリガーがほぼ同時に発火した場合、一方のみが実行される。"""

    def test_second_cron_skipped_while_first_holds_lease(self):
        """Leaseを保持したまま第2 run_cron_mode が起動 → Lease取得失敗、syncは呼ばれない。"""
        store = FakeLeaseStore()
        sync_call_count = 0

        mapping = MappingEntry("広報", "荒牧祭実行委員会", "coll-uuid", "can_view_except_passwords")
        email = "u@example.com"
        org_state = _make_org_state_matching_mapping(mapping, email)

        def make_clients():
            nonlocal sync_call_count
            sync_call_count += 1
            fake_vw = FakeVaultwardenOrgClient(
                orgs={"荒牧祭実行委員会": "org-uuid"},
                states={"荒牧祭実行委員会": org_state},
            )
            return [mapping], FakeAuthentikGroupClient({"広報": [email]}), fake_vw, FakeDiscordNotifier(), None

        with patch("subprocess.run", side_effect=store.run):
            # 第1プロセスがLeaseを取得したまま保持 (同時実行を模倣)
            occupier = SyncLockManager("prod")
            assert occupier.acquire() is True

            # 第2プロセス (run_cron_mode) → Lease取得失敗 → syncをスキップ
            lock2 = SyncLockManager("prod")
            ret2 = run_cron_mode(lock_manager=lock2, client_factory=make_clients)

        assert ret2 == 0, "run_cron_mode exits 0 even when skipped"
        assert sync_call_count == 0, "sync must not execute while first process holds Lease"

    def test_trigger_blocked_when_cron_holds_lease(self):
        """CronJobがLease保持中 → TriggerReceiverはLease取得失敗、sync未実行のまま202を返す。"""
        from sync import TriggerReceiver

        store = FakeLeaseStore()
        cron_lock = SyncLockManager("prod")
        trigger_lock = SyncLockManager("prod")
        trigger_sync_called = False

        with patch("subprocess.run", side_effect=store.run):
            acquired = cron_lock.acquire()
            assert acquired is True

            def run_sync():
                nonlocal trigger_sync_called
                trigger_sync_called = True

            receiver = TriggerReceiver(
                trigger_token="secret-token",
                lock_manager=trigger_lock,
                run_sync=run_sync,
            )
            status = receiver.handle_trigger("Bearer secret-token")

        assert status == 202
        assert not trigger_sync_called, "sync must not run when CronJob holds Lease"

    def test_lease_released_after_cron_completes_allows_next_run(self):
        """先行CronJob完了後にLeaseが解放 → 後続CronJobは取得に成功し実行される。"""
        store = FakeLeaseStore()
        run_count = 0

        mapping = MappingEntry("企画", "荒牧祭実行委員会", "coll-uuid", "can_edit")
        email = "k@example.com"
        org_state = _make_org_state_matching_mapping(mapping, email)

        def make_clients():
            nonlocal run_count
            run_count += 1
            fake_vw = FakeVaultwardenOrgClient(
                orgs={"荒牧祭実行委員会": "org-uuid"},
                states={"荒牧祭実行委員会": org_state},
            )
            return [mapping], FakeAuthentikGroupClient({"企画": [email]}), fake_vw, FakeDiscordNotifier(), None

        with patch("subprocess.run", side_effect=store.run):
            run_cron_mode(lock_manager=SyncLockManager("prod"), client_factory=make_clients)
            assert store.lease is None, "Lease should be released after first run"
            run_cron_mode(lock_manager=SyncLockManager("prod"), client_factory=make_clients)

        assert run_count == 2, "both cron runs should execute when run sequentially"


# ---------------------------------------------------------------------------
# 10.3-C: MicroTime形式回帰テスト — Lease作成にマイクロ秒タイムスタンプを使う
# ---------------------------------------------------------------------------

class TestMicroTimeFormat:
    """K8s Lease の acquireTime/renewTime は MicroTime 型 → マイクロ秒 (%f) が必要。

    2026-06-26 実機で判明: 秒のみのタイムスタンプ ("2026-06-26T05:00:03Z") は
    BadRequest になり lease_acquire_error → cron_skipped_lease_busy となっていた。
    """

    def test_lease_manifest_contains_microseconds(self):
        """_build_manifest が生成するタイムスタンプにマイクロ秒 (.xxxxxx) が含まれる。"""
        lock = SyncLockManager("prod")
        manifest = lock._build_manifest("test-holder")
        acquire_time = manifest["spec"]["acquireTime"]
        renew_time = manifest["spec"]["renewTime"]

        assert "." in acquire_time, f"acquireTime lacks microseconds: {acquire_time!r}"
        assert "." in renew_time, f"renewTime lacks microseconds: {renew_time!r}"
        assert acquire_time.endswith("Z"), f"acquireTime must end with Z: {acquire_time!r}"

    def test_utc_now_rfc3339_includes_microseconds(self):
        """_utc_now_rfc3339() が 'YYYY-MM-DDTHH:MM:SS.ffffffZ' 形式を返す。"""
        from sync import _utc_now_rfc3339
        ts = _utc_now_rfc3339()
        assert "." in ts, f"timestamp lacks microseconds: {ts!r}"
        assert ts.endswith("Z"), f"timestamp must end with Z: {ts!r}"
        # 小数点以下6桁 (マイクロ秒)
        frac = ts.split(".")[1].rstrip("Z")
        assert len(frac) == 6, f"expected 6 fractional digits, got: {frac!r}"
