# Design Document - Repository Governance & Spec-driven Lifecycle

## Overview
本ドキュメントは、リポジトリ `aramakisai-infra` の運用・保守フェーズ移行に向けた、ブランチ保護規則、仕様駆動開発（Kiro Specs）のGitブランチ連動ライフサイクル、Pull Request（PR）テンプレート、および静的解析CI（GitHub Actions）の設計仕様です。

### Goals
- 開発者が `main` ブランチへの直接コミット・プッシュを行えない状態を作り、すべての変更をコードレビューと自動検証（CI）を経てのみ反映されるようにする。
- 変更内容に応じた「ブランチ命名規則（`spec/`, `feat/`, `fix/`, `docs/`, `hotfix/`）」を制定し、Kiro Specsでの仕様・タスク合意から実装完了までの2段階マージのプロセスを強制・推進する。
- PR作成時に、実施した検証内容やインフラ影響範囲がセルフチェック・明記される仕組み（PRテンプレート）を提供する。
- ローカルで実行可能な静的解析（Linter/Validator）をPR作成・更新時に自動実行するGitHub Actionsワークフローを導入し、構文エラーのあるコードのマージを機械的に防止する。

### Non-Goals
- GitHubのアカウントやコラボレータの権限自体（Organizationの設定など）の変更。
- 本番Kubernetesクラスター内のRBAC設定、またはInfisical側の認証認可制御の設定変更。
- 自動CD（マニフェスト適用）プロセスの変更（ArgoCDの自動同期は既存のまま）。

---

## Boundary Commitments

### This Spec Owns
- `.github/pull_request_template.md` の新規追加。
- `.github/workflows/pr-validation.yml` の新規追加。
- `.kiro/specs/repo-governance/` 配下の仕様管理ファイルの更新。

### Out of Boundary
- GitHubリポジトリの設定（Branch Protection Rules）自体はGitHub Web UIから手動で適用するため、本スペックは設定手順のドキュメント化とCI側の対応のみをスコープとする（GitHub API等を用いた自動ブランチ保護設定は対象外とする）。

---

## File Structure Plan

### New Files
- [.github/pull_request_template.md](../../.github/pull_request_template.md) — レビュー品質向上のためのチェックリストとフォーマット。
- [.github/workflows/pr-validation.yml](../../.github/workflows/pr-validation.yml) — PR作成・更新時に `make lint` を実行するGitHub Actionsワークフロー。

### Modified Files
- [.kiro/specs/repo-governance/spec.json](spec.json) — 進捗ステータスおよび承認状態の更新。

---

## Architecture & Implementation Details

### 1. 2段階の仕様駆動ブランチライフサイクル
新規の機能追加や大規模変更時、Git上でのライフサイクルは以下の流れで進行します。

1. **仕様策定段階**:
   - ブランチ名: `spec/<仕様名>` (例: `spec/dr-automation`)
   - 変更内容: `.kiro/specs/<仕様名>/` 内の `spec.json` と `requirements.md`、`design.md`、`tasks.md` 等の作成。
   - PRターゲット: `main`
   - 合意形成: レビュワーが仕様・要件・実装計画を確認してマージ。この時点では実際のコード変更は含めない。
2. **実装段階**:
   - ブランチ名: `feat/<仕様名>` または `fix/<仕様名>`
   - 変更内容: 仕様書で合意された実装、および `.kiro/specs/<仕様名>/spec.json` のステータス変更（`phase: "completed"` 等）や `tasks.md` のタスク消化。
   - PRターゲット: `main`
   - 合意形成: CIチェックをパスし、要件定義を満たしていることをレビューしてマージ。

*※軽微なドキュメント修正や緊急のホットフィックスに関しては、`docs/<内容>` または `hotfix/<内容>` として仕様を挟まずに直接 `main` へのPRマージを行う例外ルートを許容します。*

### 2. PR検証CI（PR Validation）の設計
GitHub Actions で動作する検証ジョブは、既存の `Makefile` の静的解析機能を全面的に再利用し、二重管理を防ぎます。

- **トリガー条件**: `main` ブランチに対する Pull Request の `opened`, `synchronize`, `reopened` イベント。
- **検証内容**:
  1. `actions/checkout@v4` によるコード取得。
  2. `astral-sh/setup-uv@v3` による `uv` 環境のセットアップ。
  3. `make setup` の実行（`pre-commit`, `yamllint`, `ansible-lint` などのツールのインストール）。
  4. `make lint` の実行（`pre-commit run --all-files` による一括静的解析およびマニフェスト・スキーマ検証）。

---

## Testing Strategy

### 1. PRテンプレートの表示テスト
- ブランチを切り、PRテンプレートファイルが配置されたことを確認後、検証用PRを作成してテンプレートがデフォルトの本文として正しく自動読み込みされるかを確認する。

### 2. PR検証CIの動作テスト
- 意図的に静的解析エラー（例: YAMLのインデントミス、ShellScriptの構文エラー）を混入させたコミットをプッシュし、CIが正常に失敗（Red）することを確認する。
- 修正後にCIが正常に成功（Green）することを確認する。
