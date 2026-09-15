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
    EventDedupStore,
    SyncLockManager,
    WebhookHTTPServer,
    WebhookReceiver,
    build_clients_from_env,
    main,
    run_cron_mode,
    run_serve_mode,
    verify_zitadel_signature,
)


class TestEventDedupStore:
    """Requirement 4.1: at-least-once配信に対する冪等処理の重複排除ストア単体テスト"""

    def test_first_seen_key_returns_true(self):
        store = EventDedupStore()
        assert store.mark_if_new("a") is True

    def test_repeated_key_returns_false(self):
        store = EventDedupStore()
        assert store.mark_if_new("a") is True
        assert store.mark_if_new("a") is False

    def test_different_keys_both_return_true(self):
        store = EventDedupStore()
        assert store.mark_if_new("a") is True
        assert store.mark_if_new("b") is True

    def test_key_expires_after_ttl(self):
        """TTLを超えると再送でなく新規イベントとして扱う(長期のPod継続稼働でのメモリ肥大防止)。"""
        fake_now = [0.0]
        store = EventDedupStore(ttl_seconds=10.0, clock=lambda: fake_now[0])

        assert store.mark_if_new("a") is True
        fake_now[0] = 20.0
        assert store.mark_if_new("a") is True


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
            return [], object(), object(), object(), None

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
            return [], object(), object(), object(), None

        result = run_cron_mode(lock_manager=lock, client_factory=fake_client_factory)

        assert result == 0
        assert lock.calls == ["acquire"]
        assert called == []

    def test_lease_released_even_if_orchestrator_raises(self):
        """Orchestrator実行中の例外でもLeaseは解放される。"""
        lock = FakeLockManager(acquire_result=True)

        def fake_client_factory():
            return [], object(), object(), object(), None

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


def _sign(payload: bytes, signing_key: str, ts: int | None = None) -> str:
    """テスト用: verify_zitadel_signatureが受理するZITADEL-Signatureヘッダを生成する。"""
    import hashlib
    import hmac as hmac_module
    import time as time_module

    ts = ts if ts is not None else int(time_module.time())
    mac = hmac_module.new(signing_key.encode("utf-8"), digestmod=hashlib.sha256)
    mac.update(f"{ts}.".encode("utf-8"))
    mac.update(payload)
    return f"t={ts},v1={mac.hexdigest()}"


class TestWebhookReceiver:
    """Requirement 4.1, 4.2: Actions v2 webhook受信の署名検証・冪等化・Lease連携"""

    PAYLOAD = json.dumps(
        {"instanceID": "inst-1", "aggregateID": "agg-1", "sequence": 42, "event_type": "user.grant.added"}
    ).encode("utf-8")

    def test_missing_signature_returns_401(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = WebhookReceiver("signing-key", lock, run_sync=lambda: None, thread_factory=SyncThreadStub)

        status = receiver.handle_webhook(self.PAYLOAD, None)

        assert status == 401
        assert lock.calls == []

    def test_wrong_signature_returns_401(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = WebhookReceiver("signing-key", lock, run_sync=lambda: None, thread_factory=SyncThreadStub)

        status = receiver.handle_webhook(self.PAYLOAD, _sign(self.PAYLOAD, "wrong-key"))

        assert status == 401
        assert lock.calls == []

    def test_valid_signature_acquires_lease_and_runs_sync_async(self):
        """正しい署名 → 200、Lease取得成功時は非同期にsyncが起動する (4.1, 4.2)。"""
        lock = FakeLockManager(acquire_result=True)
        run_log = []
        receiver = WebhookReceiver(
            "signing-key", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )

        status = receiver.handle_webhook(self.PAYLOAD, _sign(self.PAYLOAD, "signing-key"))

        assert status == 200
        assert run_log == ["ran"]
        assert lock.calls == ["acquire", "release"]

    def test_duplicate_event_is_not_rerun(self):
        """at-least-once再送(同一イベント)は冪等に処理され、2回目はsyncを起動しない (4.1)。"""
        lock = FakeLockManager(acquire_result=True)
        run_log = []
        receiver = WebhookReceiver(
            "signing-key", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )
        header = _sign(self.PAYLOAD, "signing-key")

        status1 = receiver.handle_webhook(self.PAYLOAD, header)
        status2 = receiver.handle_webhook(self.PAYLOAD, header)

        assert status1 == 200
        assert status2 == 200
        assert run_log == ["ran"], "重複イベントでsyncが2回起動してはならない"
        assert lock.calls == ["acquire", "release"], "冪等スキップ時はLease取得すら行わない"

    def test_different_event_still_runs(self):
        """sequenceが異なる別イベントは冪等排除の対象にならない。"""
        lock = FakeLockManager(acquire_result=True)
        run_log = []
        receiver = WebhookReceiver(
            "signing-key", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )
        other_payload = json.dumps(
            {"instanceID": "inst-1", "aggregateID": "agg-1", "sequence": 43, "event_type": "user.grant.added"}
        ).encode("utf-8")

        receiver.handle_webhook(self.PAYLOAD, _sign(self.PAYLOAD, "signing-key"))
        receiver.handle_webhook(other_payload, _sign(other_payload, "signing-key"))

        assert run_log == ["ran", "ran"]

    def test_valid_signature_lease_busy_still_returns_200(self):
        """Lease取得失敗時も200を返し、次回受信での補完をログ記録するのみ。"""
        lock = FakeLockManager(acquire_result=False)
        run_log = []
        receiver = WebhookReceiver(
            "signing-key", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )

        status = receiver.handle_webhook(self.PAYLOAD, _sign(self.PAYLOAD, "signing-key"))

        assert status == 200
        assert run_log == []
        assert lock.calls == ["acquire"]

    def test_run_sync_exception_still_releases_lease(self):
        """run_sync が例外を投げてもLeaseは解放される (後続実行のブロック防止)。"""
        lock = FakeLockManager(acquire_result=True)

        def failing_run_sync():
            raise RuntimeError("sync failed")

        receiver = WebhookReceiver(
            "signing-key", lock, run_sync=failing_run_sync, thread_factory=SyncThreadStub
        )

        try:
            receiver.handle_webhook(self.PAYLOAD, _sign(self.PAYLOAD, "signing-key"))
        except RuntimeError:
            pass

        assert lock.calls == ["acquire", "release"]

    def test_invalid_json_payload_returns_400(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = WebhookReceiver("signing-key", lock, run_sync=lambda: None, thread_factory=SyncThreadStub)
        bad_payload = b"not-json"

        status = receiver.handle_webhook(bad_payload, _sign(bad_payload, "signing-key"))

        assert status == 400
        assert lock.calls == []


class TestVerifyZitadelSignature:
    """Requirement 4.1: HMAC検証ロジック単体 (zitadel-go pkg/actions/signing.go互換)"""

    def test_valid_signature_passes(self):
        payload = b'{"event_type":"user.grant.added"}'
        header = _sign(payload, "signing-key")
        verify_zitadel_signature(payload, header, "signing-key")  # raises on failure

    def test_tampered_payload_raises(self):
        import pytest

        payload = b'{"event_type":"user.grant.added"}'
        header = _sign(payload, "signing-key")
        with pytest.raises(sync.WebhookSignatureError):
            verify_zitadel_signature(b'{"event_type":"tampered"}', header, "signing-key")

    def test_expired_timestamp_raises(self):
        import pytest

        payload = b'{"event_type":"user.grant.added"}'
        header = _sign(payload, "signing-key", ts=int(__import__("time").time()) - 1000)
        with pytest.raises(sync.WebhookSignatureError):
            verify_zitadel_signature(payload, header, "signing-key")

    def test_malformed_header_raises(self):
        import pytest

        with pytest.raises(sync.WebhookSignatureError):
            verify_zitadel_signature(b"payload", "not-a-valid-header", "signing-key")


class TestWebhookHTTPServer:
    """Requirement 4.1, 4.2 (http.serverによる実HTTP動作確認)"""

    def _start_server(self, receiver: WebhookReceiver) -> tuple[WebhookHTTPServer, threading.Thread]:
        server = WebhookHTTPServer(receiver, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_valid_signature_request_returns_200(self):
        lock = FakeLockManager(acquire_result=True)
        run_log = []
        receiver = WebhookReceiver(
            "signing-key", lock, run_sync=lambda: run_log.append("ran"), thread_factory=SyncThreadStub
        )
        server, thread = self._start_server(receiver)
        try:
            payload = json.dumps({"instanceID": "i", "aggregateID": "a", "sequence": 1}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/webhook",
                data=payload,
                method="POST",
                headers={"ZITADEL-Signature": _sign(payload, "signing-key")},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert status == 200
        assert run_log == ["ran"]

    def test_invalid_signature_request_returns_401(self):
        lock = FakeLockManager(acquire_result=True)
        receiver = WebhookReceiver("signing-key", lock, run_sync=lambda: None, thread_factory=SyncThreadStub)
        server, thread = self._start_server(receiver)
        try:
            payload = json.dumps({"instanceID": "i", "aggregateID": "a", "sequence": 1}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/webhook",
                data=payload,
                method="POST",
                headers={"ZITADEL-Signature": _sign(payload, "wrong-key")},
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
        receiver = WebhookReceiver("signing-key", lock, run_sync=lambda: None, thread_factory=SyncThreadStub)
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
        monkeypatch.setenv("ZITADEL_BASE_URL", "http://zitadel.prod.svc.cluster.local")
        monkeypatch.setenv("ZITADEL_API_TOKEN", "zt-token")
        monkeypatch.setenv("VAULTWARDEN_BASE_URL", "http://vaultwarden.prod.svc.cluster.local")
        monkeypatch.setenv("VAULTWARDEN_SA_CLIENT_ID", "user.uuid")
        monkeypatch.setenv("VAULTWARDEN_SA_CLIENT_SECRET", "vw-secret")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.com/webhook")

        mappings, group_client, vaultwarden_client, discord_notifier, org_key_bytes = build_clients_from_env()

        assert len(mappings) == 1
        assert mappings[0].authentik_group == "広報"
        assert group_client._base_url == "http://zitadel.prod.svc.cluster.local"
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
