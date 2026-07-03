#!/usr/bin/env python3
"""Fail schema-sync infra PRs until the staging verification checklist is fully checked.

Used as a required status check so that `directus-schema-*` PRs (opened by the
aramakisai-web schema-sync workflow) cannot merge to main before every
"マージ前チェックリスト" item in the PR body is checked (cicd-pipeline spec 5.5).
"""
import os
import re
import sys

CHECKLIST_ITEM_RE = re.compile(r"^\s*-\s*\[( |x|X)\]\s*(.+)$", re.MULTILINE)
SCHEMA_SYNC_BRANCH_PREFIX = "directus-schema-"


def is_schema_sync_branch(branch: str) -> bool:
    return branch.startswith(SCHEMA_SYNC_BRANCH_PREFIX)


def unchecked_items(body: str) -> list[str]:
    if not body:
        return []
    return [text.strip() for mark, text in CHECKLIST_ITEM_RE.findall(body) if mark == " "]


def main() -> int:
    branch = os.environ.get("PR_HEAD_REF", "")
    if not is_schema_sync_branch(branch):
        print(f"Not a schema-sync PR ({branch!r}); skipping staging gate.")
        return 0

    body = os.environ.get("PR_BODY", "")
    remaining = unchecked_items(body)
    if remaining:
        print("Staging verification checklist is incomplete:", file=sys.stderr)
        for item in remaining:
            print(f"  - [ ] {item}", file=sys.stderr)
        return 1

    print("Staging verification checklist fully checked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
