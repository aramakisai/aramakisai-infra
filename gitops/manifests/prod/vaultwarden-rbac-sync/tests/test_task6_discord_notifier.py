"""Task 6: DiscordNotifier TDD tests.

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_task6_discord_notifier.py -v
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import DiscordNotifier


class TestDiscordNotifier:
    """Requirement 11.1, 11.2, 11.3, 11.4"""

    # -- basic notification -----------------------------------------------

    def test_notify_success(self):
        """Normal notification sends POST to webhook URL."""
        from unittest.mock import patch, MagicMock

        notifier = DiscordNotifier("https://discord.example.com/webhook")
        # sync.py imports urlopen directly, so patch sync.urlopen
        with patch("sync.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_urlopen.return_value.__enter__.return_value = mock_response
            notifier.notify("test message")

        mock_urlopen.assert_called_once()
        call_args = mock_urlopen.call_args
        request = call_args[0][0]
        assert request.full_url == "https://discord.example.com/webhook"
        body = request.data
        import json
        assert json.loads(body)["content"] == "test message"

    # -- failure resilience -----------------------------------------------

    def test_notify_failure_does_not_raise(self):
        """Notification failure must not propagate exception (Req 11.4)."""
        import urllib.request
        from unittest.mock import patch

        notifier = DiscordNotifier("https://discord.example.com/webhook")
        with patch.object(urllib.request, "urlopen", side_effect=Exception("network error")):
            # Should not raise
            notifier.notify("test message")

    def test_notify_empty_url_does_nothing(self):
        """Empty webhook URL should silently skip."""
        notifier = DiscordNotifier("")
        notifier.notify("test message")  # Should not raise

    # -- summary format ---------------------------------------------------

    def test_summary_contains_counts(self):
        """Summary includes invite/update/removal/unchanged/confirm_pending counts."""
        from sync import (
            SyncPlan, InvitePlan, UpdatePlan, RemovalPlan, ConfirmPendingPlan,
            CollectionGrant,
        )

        plan = SyncPlan(
            invites=[InvitePlan("new@example.com", "org-1", [])],
            confirm_pending=[ConfirmPendingPlan("pending@example.com", "org-1")],
            auto_confirm=[],
            collection_updates=[UpdatePlan("m1", "org-1", 2, [])],
            collection_removals=[RemovalPlan("m2", "org-1", 2, {"c1"})],
            unchanged_count=3,
        )

        # Build summary using SyncOrchestrator internal helper
        import sync
        notifier = DiscordNotifier("")
        orch = sync.SyncOrchestrator.__new__(sync.SyncOrchestrator)
        summary = orch._build_summary(plan, dry_run=False, errors=[])

        assert "招待: 1件" in summary
        assert "権限更新: 1件" in summary
        assert "権限削除: 1件" in summary
        assert "変更なし: 3件" in summary
        assert "未Accept: 1件" in summary
        assert "pending@example.com" in summary

    def test_summary_contains_errors(self):
        """Summary includes error details when errors exist (Req 11.2)."""
        from sync import SyncPlan

        plan = SyncPlan(
            invites=[], confirm_pending=[], auto_confirm=[], collection_updates=[],
            collection_removals=[], unchanged_count=0,
        )

        import sync
        notifier = DiscordNotifier("")
        orch = sync.SyncOrchestrator.__new__(sync.SyncOrchestrator)
        summary = orch._build_summary(plan, dry_run=False, errors=["invite failed: x"])

        assert "エラー:" in summary
        assert "invite failed: x" in summary

    def test_dry_run_label_in_summary(self):
        """dry-run mode prefixes summary with [dry-run]."""
        from sync import SyncPlan

        plan = SyncPlan(
            invites=[], confirm_pending=[], auto_confirm=[], collection_updates=[],
            collection_removals=[], unchanged_count=0,
        )

        import sync
        notifier = DiscordNotifier("")
        orch = sync.SyncOrchestrator.__new__(sync.SyncOrchestrator)
        summary = orch._build_summary(plan, dry_run=True, errors=[])

        assert "[dry-run]" in summary
