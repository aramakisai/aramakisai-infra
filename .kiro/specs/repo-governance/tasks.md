# Implementation Plan

## Tasks

- [ ] 1. PR検証用 CI ワークフローの追加
  - `.github/workflows/pr-validation.yml` を新規作成する。
  - トリガー条件を `main` への Pull Request (`opened`, `synchronize`, `reopened`) とする。
  - ジョブステップで `actions/checkout@v4`、`astral-sh/setup-uv@v3`、`make setup`、`make lint` を順に実行し、静的解析チェックを走らせる。
  - _Requirements: 3_
  - _Boundary: pr-validation.yml_

- [ ] 2. Pull Request テンプレートの追加
  - `.github/pull_request_template.md` を新規作成する。
  - 変更概要、関連仕様書、手動検証の実施チェックリスト、インフラ影響範囲（Hetzner / Cloudflare / シークレット / DB）のチェック項目を記述する。
  - _Requirements: 2_
  - _Boundary: pull_request_template.md_

- [ ] 3. GitHub リポジトリのブランチ保護規則設定（手動タスク）
  - GitHub Web UI の Settings -> Branches より、`main` ブランチに対して以下の保護ルールを設定する。
    - **Require a pull request before merging** を有効化。
    - **Require approvals** を有効化（必要な承認数: 1）。
    - **Dismiss stale pull request approvals when new commits are pushed** を有効化。
    - **Require status checks to pass before merging** を有効化し、ステータスチェック対象として `lint-and-validate` ジョブを指定。
  - _Requirements: 1, 4, 5_
  - _Boundary: GitHub Repository Settings (Manual)_

- [ ] 4. 動作検証およびCIの成否確認（検証タスク）
  - `repo-governance` スペックマージ後、検証用のテストブランチ（例: `feat/test-governance-ci`）を作成する。
  - わざと yamllint や shellcheck が失敗するようなエラーを混入させたコミットをプッシュして PR を作成する。
  - 以下を確認する：
    1. PR作成時にテンプレートが自動挿入されること。
    2. GitHub Actions の `PR Validation` ワークフローが走り、正常に失敗（Red）すること。
    3. ブランチ保護規則により、CI失敗時および未Approve時にマージがブロックされること。
  - エラーを修正してプッシュし、CIが成功（Green）することを確認した上で、承認・マージする。
  - _Requirements: 1, 2, 3, 5_
  - _Boundary: Verification_
