"""Task 8: 実行エントリポイントへの統合 TDD tests.

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task8_entrypoint.py -v
"""
import json
import sys
import threading
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).parent.parent))

import sync
from sync import (
    K8S_NAMESPACE,
    SyncLockManager,
    TriggerHTTPServer,
    TriggerReceiver,
    build_clients_from_env,
    main,
    run_cron_mode,
    run_serve_mode,
)


class FakeLockManager:
    def __init__(self, acquire_result: bool = True):
        self.acquire_result = acquire_result
        self.calls: list[str] = []

    def acquire(self) -> bool:
        self.calls.append("acquire")
        return self.acquire_result

    def release(self) -> None:
        self.calls.append("release")


class SyncThreadStub:
    """threading.Threadの代わりに同期的にtargetを実行するテスト用スタブ。"""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self) -> None:
        self._target()


class TestRunCronMode:
    """Requirement 10.1, 10.2, 10.3"""

    def test_lease_acquired_then_orchestrator_runs_then_released(self):
        """Lease取得→SyncOrchestrator実行→Lease解放の順に実行される。"""
        lock = FakeLockManager(acquire_result=True)
        run_log: list[str] = []

        def fake_client_factory():
            run_log.append("client_factory")
            return [], object(), object(), object()

        class FakeOrchestrator:
            def __init__(self, *args, **kwargs):
                run_log.append("orchestrator_created")

            def run(self, dry_run):
                run_log.append(f"run dry_run={dry_run}")

        original = sync.SyncOrchestrator
        sync.SyncOrchestrator = FakeOrchestrator
        try:
            result = run_cron_mode(lock_manager=lock, client_factory=fake_client_factory)
        finally:
            sync.SyncOrchestrator = original

        assert result == 0
        assert lock.calls == ["acquire", "release"]
        assert run_log == ["client_factory", "orchestrator_created", "run dry_run=False"]

    def test_lease_busy_skips_run_and_exits_cleanly(self):
        """Lease取得失敗時は実行せずexit 0で正常終了する (10.2)。"""
        lock = FakeLockManager(acquire_result=False)
        called = []

        def fake_client_factory():
            called.append(True)
            return [], object(), object(), object()

        result = run_cron_mode(lock_manager=lock, client_factory=fake_client_factory)

        assert result == 0
        assert lock.calls == ["acquire"]
        assert called == []

    def test_lease_released_even_if_orchestrator_raises(self):
        """Orchestrator実行中の例外でもLeaseは解放される。"""
        lock = FakeLockManager(acquire_result=True)

        def fake_client_factory():
            return [], object(), object(), object()

        class RaisingOrchestrator:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, dry_run):
                raise RuntimeError("boom")

        original = sync.SyncOrchestrator
        sync.SyncOrchestrator = RaisingOrchestrator
        try:
            try:
                run_cron_mode(lock_manager=lock, client_factory=fake_client_factory)
            except RuntimeError:
                pass
        finally:
            sync.SyncOrchestrator = original

        assert lock.calls == ["acquire", "release"]


class TestTriggerReceiver:
    """Requirement 13.1, 13.2, 13.3, 13.4"""

    def test_missing_bearer_returns_401(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = TriggerReceiver("secret-token", lock, run_sync=lambda: None, thread_factory=SyncThreadStub)

        status = receiver.handle_trigger(None)

        assert status == 401
        assert lock.calls == []

    def test_wrong_bearer_returns_401(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = TriggerReceiver("secret-token", lock, run_sync=lambda: None, thread_factory=SyncThreadStub)

        status = receiver.handle_trigger("Bearer wrong-token")

        assert status == 401
        assert lock.calls == []

    def test_valid_bearer_acquires_lease_and_runs_sync_async(self):
        """正しいBearerトークン→202、Lease取得成功時は非同期にsyncが起動する (13.1, 13.2)。"""
        lock = FakeLockManager(acquire_result=True)
        run_log = []
        receiver = TriggerReceiver(
            "secret-token", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )

        status = receiver.handle_trigger("Bearer secret-token")

        assert status == 202
        assert run_log == ["ran"]
        assert lock.calls == ["acquire", "release"]

    def test_valid_bearer_lease_busy_still_returns_202(self):
        """Lease取得失敗時も202を返し、次回実行での補完をログ記録するのみ (13.4)。"""
        lock = FakeLockManager(acquire_result=False)
        run_log = []
        receiver = TriggerReceiver(
            "secret-token", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )

        status = receiver.handle_trigger("Bearer secret-token")

        assert status == 202
        assert run_log == []
        assert lock.calls == ["acquire"]

    def test_run_sync_exception_still_releases_lease(self):
        """run_sync が例外を投げてもLeaseは解放される (13.3, 後続実行のブロック防止)。"""
        lock = FakeLockManager(acquire_result=True)

        def failing_run_sync():
            raise RuntimeError("sync failed")

        receiver = TriggerReceiver(
            "secret-token", lock, run_sync=failing_run_sync, thread_factory=SyncThreadStub
        )

        try:
            receiver.handle_trigger("Bearer secret-token")
        except RuntimeError:
            pass

        assert lock.calls == ["acquire", "release"]


class TestTriggerHTTPServer:
    """Requirement 13.1, 13.2 (http.serverによる実HTTP動作確認)"""

    def _start_server(self, receiver: TriggerReceiver) -> tuple[TriggerHTTPServer, threading.Thread]:
        server = TriggerHTTPServer(receiver, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_valid_bearer_request_returns_202(self):
        lock = FakeLockManager(acquire_result=True)
        run_log = []
        receiver = TriggerReceiver(
            "secret-token", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )
        server, thread = self._start_server(receiver)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/trigger",
                method="POST",
                headers={"Authorization": "Bearer secret-token"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert status == 202
        assert run_log == ["ran"]

    def test_invalid_bearer_request_returns_401(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = TriggerReceiver(
            "secret-token", lock, run_sync=lambda: None, thread_factory=SyncThreadStub
        )
        server, thread = self._start_server(receiver)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/trigger",
                method="POST",
                headers={"Authorization": "Bearer nope"},
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                assert False, "expected HTTPError"
            except HTTPError as exc:
                status = exc.code
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert status == 401

    def test_healthz_returns_200(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = TriggerReceiver(
            "secret-token", lock, run_sync=lambda: None, thread_factory=SyncThreadStub
        )
        server, thread = self._start_server(receiver)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/healthz", timeout=5
            ) as resp:
                status = resp.status
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert status == 200


class TestBuildClientsFromEnv:
    """Requirement 12.1: 環境変数・ConfigMapから各クライアントを構築する"""

    def test_builds_clients_from_env_and_mapping_file(self, tmp_path, monkeypatch):
        mapping_file = tmp_path / "mapping.json"
        mapping_file.write_text(
            json.dumps(
                {
                    "mappings": [
                        {
                            "authentik_group": "広報",
                            "organization": "SNS",
                            "collection_id": "coll-1",
                            "permission": "can_view",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setenv("MAPPING_CONFIG_PATH", str(mapping_file))
        monkeypatch.setenv("AUTHENTIK_BASE_URL", "http://authentik-server.prod.svc.cluster.local")
        monkeypatch.setenv("AUTHENTIK_API_TOKEN", "ak-token")
        monkeypatch.setenv("VAULTWARDEN_BASE_URL", "http://vaultwarden.prod.svc.cluster.local")
        monkeypatch.setenv("VAULTWARDEN_SA_CLIENT_ID", "user.uuid")
        monkeypatch.setenv("VAULTWARDEN_SA_CLIENT_SECRET", "vw-secret")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.com/webhook")

        mappings, authentik_client, vaultwarden_client, discord_notifier = build_clients_from_env()

        assert len(mappings) == 1
        assert mappings[0].authentik_group == "広報"
        assert authentik_client._base_url == "http://authentik-server.prod.svc.cluster.local"
        assert vaultwarden_client._base_url == "http://vaultwarden.prod.svc.cluster.local"
        assert discord_notifier._webhook_url == "https://discord.example.com/webhook"


class TestMainDispatch:
    """main() が --mode に応じて適切なエントリポイントを呼び出す"""

    def test_cron_mode_dispatches_to_run_cron_mode(self, monkeypatch):
        called = []
        monkeypatch.setattr(sync, "run_cron_mode", lambda: called.append("cron") or 0)

        result = main(["--mode=cron"])

        assert result == 0
        assert called == ["cron"]

    def test_serve_mode_dispatches_to_run_serve_mode(self, monkeypatch):
        called = []
        monkeypatch.setattr(sync, "run_serve_mode", lambda: called.append("serve") or 0)

        result = main(["--mode=serve"])

        assert result == 0
        assert called == ["serve"]


class TestK8sNamespaceConstant:
    def test_namespace_is_hardcoded_prod(self):
        assert K8S_NAMESPACE == "prod"
