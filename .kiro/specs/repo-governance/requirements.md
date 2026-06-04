# Requirements Document

## Introduction
本プロジェクトが0->1フェーズから運用・保守フェーズに移行するにあたり、本番環境である `main` ブランチの保護、Pull Request（PR）テンプレートによるレビュー品質の確保、および静的解析（Linter/Validator）による自動検証（CI）を整備し、レポジトリの統治（ガバナンス）を確立する。

## Boundary Context
- **In scope**:
  - `main` ブランチに対するブランチ保護規則（Branch Protection Rules）の定義
  - Pull Request作成時に使用されるテンプレート（`.github/pull_request_template.md`）の追加
  - Pull Request作成・更新時に自動実行される検証CI（GitHub Actions）の定義および追加
  - 緊急デプロイおよびDR（災害復旧）時の特例ルールの策定
- **Out of scope**:
  - Terraform Cloudにおけるアクセス権限自体の変更（GitHub連携外の権限管理）
  - 本番クラスター（Kubernetes）におけるRBACの新規設定（本スペックはレポジトリのガバナンスを対象とする）

## Requirements

### Requirement 1: main ブランチ保護ルールの定義
**Objective:** 開発者および運用者として、`main` ブランチへ不正または未検証のコードが直接マージされるのを防ぎ、本番インフラの安定性を保ちたい。

#### Acceptance Criteria
1. The GitHub repository shall `main` ブランチへの直接プッシュ（直接コミットのプッシュ）を禁止すること。
2. The GitHub repository shall マージ前に最低1名以上の管理者またはインフラ担当者によるApproveを必須とすること。
3. The GitHub repository shall レビュー後に新規コミットがプッシュされた場合、既存のApproveを自動でリセット（Dismiss stale approvals）すること。
4. The GitHub repository shall マージ前に後述する「PR検証CI（Status Check）」のパスを必須とすること。

### Requirement 2: Pull Request テンプレートの導入
**Objective:** レビュワーとして、PR作成者がインフラへの影響範囲や実行した検証内容を明確に記述し、レビュー時の判断ミスを削減したい。

#### Acceptance Criteria
1. The repository shall `.github/pull_request_template.md` を含めること。
2. PRテンプレートには、変更概要、関連するKiro Spec（`/kiro:spec-status`）、手動で実施した検証（`make lint`など）、およびインフラ影響範囲（Hetzner / Cloudflare / シークレット / データベース等への影響有無）のチェックリストが含まれること。

### Requirement 3: PR検証CIの自動化
**Objective:** 運用者として、すべてのPRがマージ前に自動的にLinter（静的解析）およびマニフェスト妥当性チェックをパスしていることを保証したい。

#### Acceptance Criteria
1. The repository shall GitHub Actionsとして `.github/workflows/pr-validation.yml` を実装すること。
2. PR検証CIは、PR作成・更新時に自動トリガーされること。
3. PR検証CIは、リポジトリに実装済みの `Makefile` 内の `make setup` および `make lint` を実行し、すべての静的解析（yamllint, ansible-lint, kubeconform, terraform_fmt, terraform_validate, shellcheck）がエラーなしで完了することを検証すること。

### Requirement 4: 緊急時およびDR時のプロトコル定義
**Objective:** 障害対応者として、緊急時（DR発動時など）に厳格な保護ルールが復旧のボトルネックにならないよう、特例的なバイパスルールおよび自動復旧との整合性を定義したい。

#### Acceptance Criteria
1. 自動復旧ワークフロー（`dr-recovery.yml`）は、Gitリポジトリへのプッシュを行わないため、ブランチ保護ルールをバイパスできる設計になっていることを確認すること。
2. 管理者による「Bypass branch protection rules」の実行基準と、実行後の事後レポート（Post-mortem）作成および自動復旧へのフィードバック手順を文書化すること。

### Requirement 5: ブランチ命名規則と仕様駆動開発ライフサイクルの連動
**Objective:** 開発・運用チームとして、すべての変更の背景となる「仕様（Kiro Specs）」と「コードの変更」の追跡可能性を確保し、行き当たりばったりの実装を防止したい。

#### Acceptance Criteria
1. 新規機能追加や大規模な変更の際は、必ず事前に対応するKiro Specを作成し、`spec/<仕様名>`（例: `spec/repo-governance`）ブランチにてスペックファイル群のPRを承認・マージすること。
2. スペックマージ後の実装フェーズでは、`feat/<仕様名>` または `fix/<仕様名>` ブランチを切り、仕様に合致した実装およびスペックステータスの完了（completed/implemented）への更新を行い、PRの承認・マージを行うライフサイクルを定義すること。
3. 仕様策定を必要としない軽微なドキュメント修正や緊急のホットフィックスに関しては、`docs/<内容>` または `hotfix/<内容>` などのブランチ名による直接のPRマージを許可すること。
