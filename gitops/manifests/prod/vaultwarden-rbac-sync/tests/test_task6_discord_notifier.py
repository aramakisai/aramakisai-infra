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

    # -- confirm pending message format ---------------------------------------------------

    def test_confirm_pending_message_contains_emails(self):
        """confirm_pending通知メッセージに未Acceptメールが含まれる。"""
        import sync
        orch = sync.SyncOrchestrator.__new__(sync.SyncOrchestrator)
        msg = orch._build_confirm_pending_message({"a@example.com", "b@example.com"})
        assert "a@example.com" in msg
        assert "b@example.com" in msg
        assert "招待" in msg

    def test_confirm_pending_message_sorted(self):
        """メールアドレスがソート順で列挙される。"""
        import sync
        orch = sync.SyncOrchestrator.__new__(sync.SyncOrchestrator)
        msg = orch._build_confirm_pending_message({"z@example.com", "a@example.com"})
        assert msg.index("a@example.com") < msg.index("z@example.com")
