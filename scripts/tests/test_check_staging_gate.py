import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_staging_gate import unchecked_items, is_schema_sync_branch  # noqa: E402

ALL_CHECKED_BODY = """
## マージ前チェックリスト

- [x] staging (namespace `staging`) の schema-apply Job が成功したことを確認した
- [x] staging Cloudflare Workers Preview URL でフロントエンド E2E 確認済み
- [x] staging Directus 管理画面で API を確認済み
- [x] 破壊的変更は含まれていない
"""

PARTIALLY_CHECKED_BODY = """
## マージ前チェックリスト

- [x] staging (namespace `staging`) の schema-apply Job が成功したことを確認した
- [ ] staging Cloudflare Workers Preview URL でフロントエンド E2E 確認済み
- [x] staging Directus 管理画面で API を確認済み
- [x] 破壊的変更は含まれていない
"""

NO_CHECKLIST_BODY = "Just a plain PR description with no checklist."


def test_unchecked_items_returns_empty_when_all_checked():
    assert unchecked_items(ALL_CHECKED_BODY) == []


def test_unchecked_items_returns_remaining_items():
    result = unchecked_items(PARTIALLY_CHECKED_BODY)
    assert len(result) == 1
    assert "E2E" in result[0]


def test_unchecked_items_empty_for_body_without_checklist():
    assert unchecked_items(NO_CHECKLIST_BODY) == []


def test_is_schema_sync_branch_matches_prefix():
    assert is_schema_sync_branch("directus-schema-abc1234")
    assert not is_schema_sync_branch("feature/some-other-change")
