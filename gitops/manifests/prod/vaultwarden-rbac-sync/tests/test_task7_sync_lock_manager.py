"""Task 7: SyncLockManager TDD tests.

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task7_sync_lock_manager.py -v

FakeLeaseStore は kubectl create/get/replace/delete を実APIサーバーのLease資源と同じ意味論
(create: 既存時はAlreadyExistsで失敗 / replace: resourceVersion不一致でConflict失敗) で
模倣する。これにより「2プロセスが同時に取得を試みた際、片方のみ成功する」という
排他制御の核心を、`kubectl apply`の上書き的upsert挙動に頼った偽陽性なしで検証できる。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import SyncLockManager


class FakeCompletedProcess:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class FakeLeaseStore:
    """kubectl create/get/replace/delete lease の実API挙動を模倣するテスト用フェイク。"""

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
                stderr="Error from server (AlreadyExists): leases.coordination.k8s.io "
                "\"vaultwarden-rbac-sync-lock\" already exists",
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


def _stale_timestamp(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestSyncLockManager:
    """Requirement 10.2, 13.3, 13.4"""

    # -- acquire: absent lease -----------------------------------------------

    def test_acquire_success_when_no_lease_exists(self):
        """Leaseが存在しない → createが成功しTrueを返す。"""
        from unittest.mock import patch

        store = FakeLeaseStore()
        lock = SyncLockManager("prod")
        with patch("subprocess.run", side_effect=store.run):
            result = lock.acquire()

        assert result is True
        assert store.lease is not None
        assert store.lease["spec"]["holderIdentity"]

    # -- acquire: fresh lease held by other ----------------------------------

    def test_acquire_fails_when_fresh_lease_held_by_other(self):
        """新鮮な(staleでない)Leaseが既に存在 → Falseを返し、既存Leaseは変更されない。"""
        from unittest.mock import patch

        store = FakeLeaseStore()
        store.lease = {
            "metadata": {"name": "vaultwarden-rbac-sync-lock", "resourceVersion": "1"},
            "spec": {
                "holderIdentity": "other-process",
                "leaseDurationSeconds": 600,
                "renewTime": _stale_timestamp(5),
            },
        }
        lock = SyncLockManager("prod")
        with patch("subprocess.run", side_effect=store.run):
            result = lock.acquire()

        assert result is False
        assert store.lease["spec"]["holderIdentity"] == "other-process"

    # -- acquire: stale lease takeover ---------------------------------------

    def test_acquire_steals_stale_lease(self):
        """leaseDurationSeconds超過のLease → resourceVersionを伴うreplaceで奪取しTrueを返す。"""
        from unittest.mock import patch

        store = FakeLeaseStore()
        store.lease = {
            "metadata": {"name": "vaultwarden-rbac-sync-lock", "resourceVersion": "1"},
            "spec": {
                "holderIdentity": "crashed-process",
                "leaseDurationSeconds": 600,
                "renewTime": _stale_timestamp(900),
            },
        }
        lock = SyncLockManager("prod")
        with patch("subprocess.run", side_effect=store.run):
            result = lock.acquire()

        assert result is True
        assert store.lease["spec"]["holderIdentity"] != "crashed-process"

    # -- release --------------------------------------------------------------

    def test_release_deletes_lease_when_holder_matches(self):
        """release()は自分が取得したLeaseのみ削除する。"""
        from unittest.mock import patch

        store = FakeLeaseStore()
        lock = SyncLockManager("prod")
        with patch("subprocess.run", side_effect=store.run):
            assert lock.acquire() is True
            lock.release()

        assert store.lease is None

    def test_release_skips_when_lease_stolen_by_other(self):
        """staleとして他プロセスに奪取された後は、誤って削除しない。"""
        from unittest.mock import patch

        store = FakeLeaseStore()
        lock = SyncLockManager("prod")
        with patch("subprocess.run", side_effect=store.run):
            assert lock.acquire() is True
            # 他プロセスがstale奪取したことを模擬 (resourceVersionも更新)
            store.lease["spec"]["holderIdentity"] = "thief-process"
            store.lease["metadata"]["resourceVersion"] = "999"

            lock.release()

        assert store.lease is not None
        assert store.lease["spec"]["holderIdentity"] == "thief-process"

    # -- concurrency: only one of two racing processes acquires ---------------

    def test_two_processes_race_only_one_acquires(self):
        """2プロセスが同時にLease取得を試みた際、片方のみ取得に成功する (10.2, 13.3)。"""
        from unittest.mock import patch

        store = FakeLeaseStore()
        lock1 = SyncLockManager("prod")
        lock2 = SyncLockManager("prod")

        with patch("subprocess.run", side_effect=store.run):
            r1 = lock1.acquire()
            r2 = lock2.acquire()

        assert r1 is True
        assert r2 is False

    # -- hardcoded values -------------------------------------------------------

    def test_lease_name_and_namespace(self):
        """Lease name and namespace are hardcoded."""
        lock = SyncLockManager("prod")
        assert lock.lease_name == "vaultwarden-rbac-sync-lock"
        assert lock.namespace == "prod"
