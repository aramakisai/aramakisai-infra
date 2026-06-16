# Research & Design Decisions

---
**Purpose**: 設計判断の根拠と調査結果を記録する。
---

## Summary
- **Feature**: `b2-to-hetzner-migration`
- **Discovery Scope**: Extension (既存設定の storage backend 差し替え。新サービス追加なし)
- **Key Findings**:
  - Hetzner Object Storage は S3 互換 API を提供。エンドポイント形式は `https://<location>.your-objectstorage.com`。プロジェクトのデータセンターロケーションは `fsn1` のため `https://fsn1.your-objectstorage.com` を使用。
  - CNPG barman の `bootstrap.recovery` は S3 上にベースバックアップが存在しない場合エラーになる。0→1 フェーズでは `bootstrap.initdb` への変更が必須。
  - VolSync restic の `RESTIC_REPOSITORY` はすでに Infisical 管理 (`MAILSERVER_RESTIC_REPOSITORY`) のため、Infisical 値の更新のみでマニフェスト上の URL 変更は不要。認証情報キー名 (`B2_KEY_ID` → `HETZNER_OS_ACCESS_KEY_ID`) のみマニフェスト変更が必要。
  - `b2-external-secret.yaml` は ArgoCD Application `cluster-secret-store` に `prune: true` で管理されているため、ファイル削除のみで `b2-credentials` Secret が自動削除される。

## Research Log

### Hetzner Object Storage の S3 互換性

- **Context**: CNPG (barman) と VolSync (restic) はいずれも AWS S3 SDK または S3 互換プロトコルを使用。Hetzner が必要な API を実装しているか確認。
- **Findings**:
  - Hetzner Object Storage は S3 互換 API を実装 (AWS Signature V4 対応)
  - エンドポイント: `https://<location>.your-objectstorage.com`
  - 利用可能リージョン: `fsn1` (Falkenstein), `nbg1` (Nuremberg), `hel1` (Helsinki)
  - プロジェクトは `fsn1` を使用 (`terraform/variables.tf` `default = "fsn1"`)
  - 確定エンドポイント: `https://fsn1.your-objectstorage.com`
- **Implications**: CNPG の `endpointURL` と VolSync の `RESTIC_REPOSITORY` URL を `backblazeb2.com` から `fsn1.your-objectstorage.com` に置き換えるだけで互換動作する。

### CNPG bootstrap モード選択

- **Context**: 現在の `db-cluster.yaml` は `bootstrap.recovery` + `externalClusters` で B2 からリカバリーする設計。Hetzner に切り替えると初期状態ではバックアップが存在しない。
- **Findings**:
  - CNPG `bootstrap.recovery` は `externalClusters` で指定した S3 パスにベースバックアップが存在しないとクラスター起動に失敗する
  - `cnpg.io/skipEmptyWalArchiveCheck` は WAL **書き込み先** のチェックをスキップするが、**リカバリー元** のバックアップ不在には効かない
  - 0→1 フェーズ (既存 Hetzner バックアップなし) では `bootstrap.initdb` が唯一の正解
- **Implications**:
  - `bootstrap.recovery` → `bootstrap.initdb` への変更が必須
  - `externalClusters` セクションは削除する (空のリカバリー元参照を残さない)
  - 将来の DR 復旧フローでは Hetzner にバックアップが蓄積されてから `bootstrap.recovery` に戻す (このタイミングは dr-automation spec で管理)

### VolSync restic リポジトリ URL の管理場所

- **Context**: `restic-external-secret.yaml` は `RESTIC_REPOSITORY` を Infisical の `MAILSERVER_RESTIC_REPOSITORY` から取得している。URL をどこで管理するか確認。
- **Findings**:
  - `RESTIC_REPOSITORY` 値 (完全な S3 URL) は Infisical に保存されている
  - マニフェスト (`restic-external-secret.yaml`) は Infisical キー名を参照するだけ
  - URL 変更は **Infisical 上の値の更新のみ** で完結する
  - マニフェスト変更が必要なのは認証情報の参照キー名 (`B2_KEY_ID` → `HETZNER_OS_ACCESS_KEY_ID` 等) のみ
- **Implications**: マニフェスト変更最小化。Infisical 値の更新 (`MAILSERVER_RESTIC_REPOSITORY`) が必須の運用ステップになる。

### ArgoCD prune 動作と移行安全性

- **Context**: `b2-external-secret.yaml` の削除で既存 `b2-credentials` Secret がどう扱われるか確認。
- **Findings**:
  - ArgoCD Application `cluster-secret-store` は `prune: true` で設定されている
  - `b2-external-secret.yaml` を削除してマージすると、ArgoCD が次の sync で ExternalSecret `b2-credentials` を削除する
  - ESO が ExternalSecret を削除すると、管理対象 Secret (`b2-credentials`) も削除される (`creationPolicy: Owner`)
  - CNPG / VolSync がまだ `b2-credentials` を参照していると、削除後に認証エラーになる
- **Implications**: **アトミックに 1 PR で全変更を適用する**必要がある。`b2-credentials` 削除と `hetzner-os-credentials` 作成を同一 PR に含め、db-cluster / restic-external-secret の参照先変更も同時に行う。

### Hetzner Object Storage バケット作成の制約

- **Context**: `terraform/storage.tf` コメントに「hcloud_object_storage_bucket は provider 非サポート」と記載あり。
- **Findings**:
  - `hetznercloud/hcloud` provider (v1.50) は object storage bucket リソースを提供していない
  - バケットは Hetzner Robot (console.hetzner.com) または Hetzner S3 API で手動作成が必要
  - バケット名は `aramakisai-backups` (B2 と同名で継続使用可能)
- **Implications**: 要件 1 はすべて手動運用ステップ。タスクとして明示的に記述する必要がある。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations |
|--------|-------------|-----------|---------------------|
| アトミック 1 PR 移行 (採用) | 全 YAML 変更を 1 PR で同時適用 | 中間状態なし、ロールバックが明確 | ArgoCD sync 中に短時間 b2-credentials が存在しない可能性 |
| 段階的移行 (見送り) | hetzner-os-credentials を先に作成し、参照先を徐々に切り替え | ダウンタイムなし | 0→1 フェーズでは不要な複雑さ。中間状態の管理コストが高い |

## Design Decisions

### Decision: CNPG bootstrap モードを initdb に変更

- **Context**: Hetzner には既存バックアップが存在しないため `bootstrap.recovery` は失敗する
- **Alternatives Considered**:
  1. `recovery` + `skipEmptyWalArchiveCheck` — リカバリー元の不在には効かないため不可
  2. `initdb` (選択) — 新規 DB として起動し Hetzner へのバックアップを開始
- **Selected Approach**: `bootstrap.initdb` に変更し `externalClusters` を削除
- **Rationale**: 0→1 フェーズにつきリカバリーすべきデータなし。initdb が最も単純で正確
- **Trade-offs**: Hetzner にバックアップが蓄積されるまで DR 復旧は `initdb` のみ (データ損失リスクあり)。RTO は backup spec の DR 手順更新で明確化する
- **Follow-up**: 初回バックアップ完了後、`bootstrap.initdb` → `bootstrap.recovery` + `externalClusters` に戻すタスクを dr-automation spec に追加する

### Decision: Secret 名は hetzner-os-credentials に変更

- **Context**: `b2-credentials` という名前は新ストレージと意味が合わない
- **Alternatives Considered**:
  1. `b2-credentials` のまま再利用 — Infisical キーの参照先だけ変更。名前が誤解を招く
  2. `hetzner-os-credentials` (選択) — 正確な名称で将来の混乱を防ぐ
- **Selected Approach**: 新 ExternalSecret で `hetzner-os-credentials` Secret を作成し、全参照先を更新
- **Rationale**: 名前が役割を正確に反映していないと将来の担当者が混乱する可能性がある

## Risks & Mitigations

- CNPG `initdb` 変更でデータが消失するリスク — 0→1 フェーズかつ既存データなしのため許容。ただし本番データが存在する場合は適用前に確認必須
- Hetzner バケット作成前に ArgoCD sync が走ると CNPG/VolSync が認証エラーになるリスク — タスクの順序: Infisical 登録 → Hetzner バケット作成 → PR マージ
- restic 新規リポジトリ初期化 (`restic init`) が VolSync mover によって自動実行されることを前提としているが、失敗した場合 — VolSync は restic repo が存在しない場合に自動 init する仕様あり。万一失敗した場合はノードに SSH して手動 init する

## References

- Hetzner Object Storage (S3 互換): `https://docs.hetzner.com/storage/object-storage/`
- Hetzner Object Storage エンドポイント一覧: `https://docs.hetzner.com/storage/object-storage/getting-started/creating-a-bucket`
- CNPG bootstrap recovery: `https://cloudnative-pg.io/documentation/current/bootstrap/`
- VolSync restic mover: `https://volsync.readthedocs.io/en/stable/usage/restic/`
- restic S3 バックエンド: `https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html#amazon-s3`
