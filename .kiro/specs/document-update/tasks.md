# Implementation Plan - 実装とドキュメントの乖離解消 (document-update)

## Tasks

- [x] 1. 最新インフラコードと主要ドキュメントの乖離調査（Research）
- [x] 1.1 Terraform 構成とドキュメントの照合 (P)
  - `terraform/` 配下の最新の出力（`Healthchecks.io` 関連、`Netdata` 関連など）およびプロバイダー設定と、`tech.md` の記述を比較し、差分を特定する。
  - _Requirements: 1_
- [x] 1.2 Ansible 構成・変数とドキュメントの照合 (P)
  - `ansible/` 配下のプレイブック、ロール（特に新設された `swap` ロールや extra_args）と、`tech.md`、`structure.md`、`README.md` の記述を比較し、差分を特定する。
  - _Requirements: 1_
- [x] 1.3 GitOps (ArgoCD) マニフェストとドキュメントの照合 (P)
  - `gitops/manifests/prod/` 配下の各アプリケーション設定（特に `directus-db` のメモリリミット 512Mi、CNPG Postgres 16.8 のイメージ指定、各サービスの通信誤検知除外設定など）と、`tech.md`、`dr.md`、`README.md` の記述を比較し、差分を特定する。
  - _Requirements: 1_
- [x] 1.4 検出された乖離（ギャップ）のレポート整理
  - 照合結果に基づき、古い記述や修正が必要な項目を整理したドキュメント `research.md` を `document-update` の spec ディレクトリ内に作成する。
  - _Requirements: 1_

- [x] 2. 主要ドキュメントの修正および同期（Implementation）
- [x] 2.1 `CLAUDE.md` の修正・スリム化
  - 不要になった Kiro ルールなどの重複記述を削除し、[GEMINI.md](../../../GEMINI.md) への参照リンクに一本化する。
  - インフラ変更時に同期すべきドキュメント一覧を示すガイドライン（チェックリスト）を追記する。
  - _Requirements: 2, 3_
  - _Boundary: CLAUDE.md_
- [x] 2.2 `README.md` の修正・最新化
  - 3ノードHA構成（`cp-node`, `prod-node-1/2`）の記述やアーキテクチャ図を、シングルノード（`prod-node-1` 1台のみ、CX33）の記述に書き換える。
  - デプロイされるサービス表のメールサーバーを `Stalwart` から `Docker Mailserver (DMS)` に変更する。
  - ディレクトリ構造図に `ansible/roles/swap` を追加する。
  - _Requirements: 2_
  - _Boundary: README.md_
- [x] 2.3 `.kiro/steering/tech.md` の修正・同期
  - `directus-db` のメモリ制限（512Mi）、Ansible による swap 設定（4GB、`fail-swap-on=false`）、Terraform の出力パラメータ、監視関連の除外設定を最新のコードと同期させる。
  - _Requirements: 2_
  - _Boundary: .kiro/steering/tech.md_
- [x] 2.4 `.kiro/steering/structure.md` の修正・同期
  - `ansible/roles/swap` が追加されたため、Ansible のディレクトリ構成図および説明に `swap` ロールを追記する。
  - _Requirements: 2_
  - _Boundary: .kiro/steering/structure.md_
- [x] 2.5 `.kiro/steering/dr.md` の修正・同期
  - `dr-trigger.sh` や `test-dr-trigger-logic.sh` の最新動作と整合させる。
  - `directus-db` が PostgreSQL 16.8 を使用し、WALアーカイブからの B2 復元（`bootstrap.recovery`）を行うように更新されていることを明記。
  - _Requirements: 2_
  - _Boundary: .kiro/steering/dr.md_

- [x] 3. 変更内容の整合性検証（Validation）
- [x] 3.1 ドキュメント内相対パスリンクの検証
  - `python scripts/check-links.py` を実行し、すべての Markdown ドキュメント内のリンクが有効であることを確認する。
  - _Requirements: 2_
- [x] 3.2 機密情報の漏洩チェック
  - `uv run python scripts/check-confidential-info.py` を実行し、機密情報の混入がないことを確認する。
  - _Requirements: 2_
- [x] 3.3 全ファイルの linter 実行
  - `pre-commit run --all-files` （または `make lint`）を実行し、エラーが出ないことを確認する。
  - _Requirements: 2_
