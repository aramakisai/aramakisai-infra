"""ZitadelGroupClient TDD tests (task 4.1: authentik固有APIへの依存を除去).

Run: python3 -m pytest gitops/manifests/prod/vaultwarden-rbac-sync/tests/test_zitadel_group_client.py -v
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from sync import ZitadelApiError, ZitadelGroupClient


class TestZitadelGroupClient:
    """Requirement 4.1: Management API `/management/v1/users/grants/_search` 経由でのメンバー取得"""

    def test_get_group_members_returns_emails(self):
        client = ZitadelGroupClient(base_url="http://zitadel.prod.svc.cluster.local", api_token="pat-token")
        response_body = json.dumps(
            {"result": [{"userId": "u1", "email": "a@example.com"}, {"userId": "u2", "email": "b@example.com"}]}
        ).encode("utf-8")

        with patch("sync.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = response_body
            mock_urlopen.return_value.__enter__.return_value = mock_response

            result = client.get_group_members("pr")

        assert result.error is None
        assert result.member_emails == ["a@example.com", "b@example.com"]

        # roleKeyQueryで絞り込み、PATでBearer認証していることを確認する
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "http://zitadel.prod.svc.cluster.local/management/v1/users/grants/_search"
        assert request.get_header("Authorization") == "Bearer pat-token"
        body = json.loads(request.data)
        assert body["queries"][0]["roleKeyQuery"]["roleKey"] == "pr"

    def test_no_grants_returns_empty_without_error(self):
        """ロール未定義・0件付与のいずれも空配列で返るため、errorを立てず空リストとして扱う。"""
        client = ZitadelGroupClient(base_url="http://zitadel.prod.svc.cluster.local", api_token="pat-token")

        with patch("sync.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps({"result": []}).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_response

            result = client.get_group_members("nonexistent")

        assert result.error is None
        assert result.member_emails == []

    def test_grants_without_email_are_skipped(self):
        client = ZitadelGroupClient(base_url="http://zitadel.prod.svc.cluster.local", api_token="pat-token")

        with patch("sync.urlopen") as mock_urlopen:
            mock_response = MagicMock()
            mock_response.read.return_value = json.dumps(
                {"result": [{"userId": "u1"}, {"userId": "u2", "email": "b@example.com"}]}
            ).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_response

            result = client.get_group_members("pr")

        assert result.member_emails == ["b@example.com"]

    def test_http_error_raises_zitadel_api_error(self):
        from urllib.error import HTTPError

        client = ZitadelGroupClient(base_url="http://zitadel.prod.svc.cluster.local", api_token="bad-token")

        with patch("sync.urlopen", side_effect=HTTPError("url", 401, "unauthorized", None, None)):
            try:
                client.get_group_members("pr")
                assert False, "expected ZitadelApiError"
            except ZitadelApiError as exc:
                assert "401" in str(exc)

    def test_timeout_raises_zitadel_api_error(self):
        client = ZitadelGroupClient(base_url="http://zitadel.prod.svc.cluster.local", api_token="pat-token")

        with patch("sync.urlopen", side_effect=TimeoutError("timed out")):
            try:
                client.get_group_members("pr")
                assert False, "expected ZitadelApiError"
            except ZitadelApiError:
                pass
