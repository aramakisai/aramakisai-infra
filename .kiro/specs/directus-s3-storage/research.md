# 調査ログ - Directus S3 アセットストレージ移行

## Summary
- **Feature**: `directus-s3-storage`
- **Discovery Scope**: Extension (既存 Directus デプロイメントのストレージ設定変更)
- **Key Findings**:
  - Directus は `directus/directus:11.1.2` イメージに `@directus/storage-driver-s3` を標準搭載済みであり、新規依存追加は不要。`STORAGE_LOCATIONS=s3` と `STORAGE_S3_*` 環境変数群のみで切替可能。
  - Hetzner Object Storage は **virtual-hosted-style** (`https://<bucket>.<location>.your-objectstorage.com`) を前提としたドキュメント構成であり、`aramakisai-backups` バケットの認証情報・エンドポイント (`https://fsn1.your-objectstorage.com`) は既存の CNPG/VolSync 設定 (`hetzner-os-credentials`) をそのまま再利用できる。新規 Infisical キー登録は不要。
  - Hetzner Object Storage はバージョニングと `NoncurrentVersionExpiration` ライフサイクルルールをサポートするが、**Terraform プロバイダー非対応**のため `mc` (MinIO Client) または AWS CLI による手動設定が必須。ライフサイクルルールはバージョニング有効時のみ機能する (suspended 状態では無効)。
  - Directus はファイル配信を常に自前の `/assets/:id` エンドポイント経由でプロキシする (ストレージドライバの認証情報でバックエンドから取得しクライアントへストリーミング)。**バケットや個別オブジェクトを public-read にする必要はない** — `aramakisai-backups` バケットには CNPG WAL アーカイブも同居しているため、バケット全体を public にすると重大な情報漏洩になる。private バケットのまま運用することが安全かつ十分。

## Research Log

### Directus S3 ストレージドライバの環境変数仕様
- **Context**: REQ-01 でローカル PVC から S3 への切替が必要。Directus 11.1.2 で要求される環境変数の正確な名前・既定値を確認する必要があった。
- **Sources Consulted**:
  - [Files | Directus Docs](https://directus.com/docs/configuration/files)
  - [S3 Storage - Directus (mintlify mirror)](https://mintlify.wiki/directus/directus/storage/s3)
- **Findings**:
  - `STORAGE_LOCATIONS` はカンマ区切りのロケーション名一覧 (例: `s3`)。各ロケーション名を大文字化したプレフィックスで個別変数を指定する (`STORAGE_<LOCATION>_*`)。
  - 必須/主要変数: `STORAGE_<LOCATION>_DRIVER=s3`, `STORAGE_<LOCATION>_KEY`, `STORAGE_<LOCATION>_SECRET`, `STORAGE_<LOCATION>_BUCKET`, `STORAGE_<LOCATION>_REGION`, `STORAGE_<LOCATION>_ENDPOINT` (既定値 `s3.amazonaws.com`)、`STORAGE_<LOCATION>_ROOT` (バケット内パスプレフィックス)。
  - 非 AWS の S3 互換エンドポイントを使う場合、`STORAGE_<LOCATION>_ENDPOINT` をプロトコル付きで上書きする。`STORAGE_<LOCATION>_FORCE_PATH_STYLE` (既定 `false`) は virtual-hosted-style か path-style かを切り替える。
  - `STORAGE_<LOCATION>_ACL` も指定可能だが、本機能では使用しない (下記「Directus のアセット配信方式」参照)。
- **Implications**: `deployment.yaml` に `STORAGE_LOCATIONS=s3`、`STORAGE_S3_DRIVER=s3`、`STORAGE_S3_BUCKET=aramakisai-backups`、`STORAGE_S3_ROOT=directus-uploads`、`STORAGE_S3_ENDPOINT=https://fsn1.your-objectstorage.com`、`STORAGE_S3_REGION=fsn1`、`STORAGE_S3_FORCE_PATH_STYLE=false` を設定する。`STORAGE_S3_KEY`/`STORAGE_S3_SECRET` は ExternalSecret 経由で注入する。

### Hetzner Object Storage のアドレッシング方式とリージョン値
- **Context**: aws-sdk-s3 (Directus 内部で使用) はリージョン文字列を必須とするが、Hetzner Object Storage の「正しい」リージョン値が公開ドキュメントに明記されていない。
- **Sources Consulted**:
  - [Object Storage Overview - Hetzner Docs](https://docs.hetzner.com/storage/object-storage/overview)
- **Findings**:
  - エンドポイントは `https://<bucket-name>.<location>.your-objectstorage.com/<file-name>` 形式 (virtual-hosted-style)。利用可能ロケーションは `fsn1` (Falkenstein) / `nbg1` (Nuremberg) / `hel1` (Helsinki)。`aramakisai-backups` は `fsn1`。
  - SDK の region パラメータに使うべき正式な値はドキュメントに明記なし。
- **Implications**: 既存の CNPG `barmanObjectStore.endpointURL` 設定 (`https://fsn1.your-objectstorage.com`、region 未指定) が稼働実績ありのため、Directus 側も同一エンドポイントを採用する。`STORAGE_S3_REGION` は aws-sdk-s3 の必須パラメータ要件を満たすためロケーションコード `fsn1` を指定する。**実装時に実機アップロードで検証必須** (Open Questions / Risks に記載)。

### Hetzner Object Storage のバージョニング・ライフサイクル設定
- **Context**: REQ-02 で誤削除防止のためのバージョニングと 30 日ライフサイクルが必要。Terraform 非対応のため手動設定の正確な手順を確認する必要があった。
- **Sources Consulted**:
  - [Applying lifecycle policies - Hetzner Docs](https://docs.hetzner.com/storage/object-storage/howto-protect-objects/manage-lifecycle/)
  - [Buckets & objects FAQ - Hetzner Docs](https://docs.hetzner.com/storage/object-storage/faq/buckets-objects/)
- **Findings**:
  - バージョニングは `mc`(MinIO Client) 等の S3 互換クライアントで有効化する。Terraform Hetzner provider はバケット自体を管理できない (`terraform/storage.tf` に既存の注記あり)。
  - ライフサイクルルールはバージョニング有効時のみ機能し、サスペンド状態では無効。Hetzner は `NoncurrentVersionExpiration.NoncurrentDays` のみサポート (日付ベースの `Date` 指定は不可)。
  - 設定例:
    ```json
    {
      "Rules": [{
        "ID": "noncurrent-expiry",
        "Status": "Enabled",
        "Prefix": "",
        "NoncurrentVersionExpiration": { "NoncurrentDays": 30 }
      }]
    }
    ```
    `mc ilm rule import <alias>/<bucket> < expiry.json` または `aws s3api put-bucket-lifecycle-configuration` で適用。
  - 削除時はバージョニング有効バケットでは「削除マーカー」が付与されるのみで実体は保持される (REQ-02.2 を満たす)。
- **Implications**: バケットレベルの設定 (`mc version enable`, `mc ilm rule import`) のため、IaC ではなく手動運用手順として design.md に記載する。`Prefix: ""` を指定するとバケット全体 (CNPG WAL / VolSync バックアップ含む) にライフサイクルが適用される点に注意— WAL/restic は別途 `retentionPolicy`/`pruneIntervalDays` で独自にクリーンアップされるため、ライフサイクルの 30日 NoncurrentVersionExpiration が重複してもデータ損失リスクはない (削除済み/上書き済みオブジェクトのみに適用されるため)。

### Directus のアセット配信方式 (public URL の扱い)
- **Context**: REQ-01.2 は「公開 URL から正常に閲覧できること」を要求する。`aramakisai-backups` バケットには CNPG WAL アーカイブも同居しており、バケット全体を public にすると機密データ (DB バックアップ) が漏洩するリスクがある。
- **Sources Consulted**: Directus コアアーキテクチャの既知の挙動 (`/assets/:id` エンドポイントがストレージドライバ経由でファイルをプロキシ配信する設計、v9 以降不変)。
- **Findings**: Directus はブラウザに直接バケット URL を渡さない。すべてのアセットリクエストは Directus API (`PUBLIC_URL` 配下の `/assets/:id`) を経由し、Directus がストレージドライバの認証情報でバックエンドから取得してレスポンスとして返す。これはローカルストレージでも S3 でも同じ挙動であり、ストレージバックエンド切替による公開範囲の変化はない。
- **Implications**: `aramakisai-backups` バケットは **private のまま運用する**。`STORAGE_S3_ACL` は設定不要。「公開 URL から閲覧可能」という要件は Directus 既存の `directus_files` コレクションに対する public ロールのパーミッション設定 (本機能のスコープ外、既存動作を継続) によって満たされる。実装後に実機で `https://aramakisai.com/assets/<file-id>` からの閲覧を smoke test する (Validation Hook)。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| S3 直接書き込み (採用) | Directus コンテナが `STORAGE_LOCATIONS=s3` で Hetzner OS に直接読み書き | PVC 不要、DR で復元手順がゼロになる、既存の `hetzner-os-credentials` を再利用できる | アップロード/閲覧のレイテンシが PVC よりわずかに増える可能性 | requirements.md の方針と一致 |
| PVC + VolSync 定期バックアップ (不採用) | 現状の PVC 構成を維持し VolSync で Hetzner にバックアップ | 変更量が小さい | RPO がバックアップ間隔に依存し「即時」を満たさない。PVC リストア手順が DR に残る (REQ-04 と矛盾) | requirements.md の Goal (RTO 0分) を満たせないため不採用 |

## Design Decisions

### Decision: 既存 `hetzner-os-credentials` 認証情報チェーンの再利用
- **Context**: Directus が S3 書き込みに使う access key / secret key をどこから取得するか。
- **Alternatives Considered**:
  1. Directus 専用の新規 Hetzner アクセスキーを発行し、新しい Infisical キー (`DIRECTUS_S3_*`) を登録する
  2. 既存の `HETZNER_OS_ACCESS_KEY_ID` / `HETZNER_OS_SECRET_ACCESS_KEY` (CNPG/VolSync と共用) を再利用する
- **Selected Approach**: 案2。`directus-secrets` ExternalSecret に `STORAGE_S3_KEY` / `STORAGE_S3_SECRET` という `secretKey` を追加し、`remoteRef` は既存の `HETZNER_OS_ACCESS_KEY_ID` / `HETZNER_OS_SECRET_ACCESS_KEY` を指す。
- **Rationale**: 同一バケット (`aramakisai-backups`) 内の別プレフィックスへの書き込みであり、アクセスキーを分ける運用上のメリットが薄い (0→1 フェーズ・単独メンテナー体制)。新規キー発行は手動運用タスクを増やすだけ。
- **Trade-offs**: Directus の認証情報が CNPG/VolSync と同一権限になる (バケット全体への read/write)。将来 IAM 的な権限分離が必要になった場合は別キー発行で対応可能。
- **Follow-up**: なし (既存キーは prod 環境に登録済み、`b2-to-hetzner-migration` で動作確認済み)。

### Decision: バケットレベルの公開設定は変更しない (private 維持)
- **Context**: REQ-01.2 の「公開 URL から閲覧可能」要件を満たす方法。
- **Alternatives Considered**:
  1. バケットまたは `directus-uploads/` 配下オブジェクトを `public-read` ACL にする
  2. バケットは private のままにし、Directus の `/assets` プロキシ経由でのみ配信する
- **Selected Approach**: 案2。
- **Rationale**: 上記「Directus のアセット配信方式」調査の通り、Directus は常にプロキシ配信するため ACL 変更は不要。バケット全体を public にすると同居する CNPG WAL アーカイブが漏洩するため、私案1 は重大なセキュリティリスクとなる。
- **Trade-offs**: なし (案2はリスクのみ低減し、要件も満たす)。
- **Follow-up**: 実装後に `https://aramakisai.com/assets/<file-id>` への匿名アクセスで実機確認する。

### Decision: PVC 削除前に一回限りの移行手順を文書化する (Job/rclone 手動実行)
- **Context**: REQ-03 で既存 PVC 内アセットの安全な移行手順確立が必要。`directus-uploads` PVC は ArgoCD が管理するため、`deployment.yaml` から PVC 定義を削除すると `prune: true` により基盤データが削除される。
- **Alternatives Considered**:
  1. 恒久的な migration Job マニフェストを gitops に追加する
  2. ドキュメント化された一回限りの CLI 手順 (`docs/` 配下) として残し、gitops には残さない
- **Selected Approach**: 案2。`docs/directus-s3-migration.md` に rclone ベースの手順を記載し、実行はローカルから `make kubectl` 経由で一時 Pod を起動して行う。
- **Rationale**: 一度しか実行しない作業を永続的な GitOps リソースとして残すと不要な複雑性が増す。既存の `scripts/test-cnpg-restore.sh` 等も一回限りの検証スクリプトであり、永続マニフェスト化されていない。
- **Trade-offs**: 手順の再現性はドキュメント品質に依存する。
- **Follow-up**: 移行完了後、PVC 削除前に Hetzner OS 側でファイル数/容量が PVC 側と一致することを目視確認する。

## Risks & Mitigations
- `STORAGE_S3_REGION` の正式な値が Hetzner 公式ドキュメントで明記されていない (`fsn1` を仮定) — 実装後に実機アップロードで検証し、失敗時は空文字列 `""` または `auto` を試す。
- PVC 削除 (`prune: true`) は不可逆操作であり、移行手順実行前に PR をマージすると PVC ごとアセットが消える — 「PR マージは移行完了確認後」という順序制約を design.md の Migration Strategy に明記し、誤マージを防ぐ。
- ライフサイクルルールの `Prefix: ""` はバケット全体に適用されるため、CNPG/VolSync のオブジェクトにも 30日 NoncurrentVersionExpiration が及ぶ — CNPG はバージョニング非対応の上書きのみ (バージョニングが有効になるとバックアップオブジェクトも旧バージョン扱いになる可能性) のため、実装時に CNPG WAL アーカイブが想定外に削除されないか確認する。

## References
- [Files | Directus Docs](https://directus.com/docs/configuration/files) — S3 ストレージ環境変数の正式リファレンス
- [Object Storage Overview - Hetzner Docs](https://docs.hetzner.com/storage/object-storage/overview) — エンドポイント形式・ロケーションコード
- [Applying lifecycle policies - Hetzner Docs](https://docs.hetzner.com/storage/object-storage/howto-protect-objects/manage-lifecycle/) — `NoncurrentVersionExpiration` 設定手順
- [Buckets & objects FAQ - Hetzner Docs](https://docs.hetzner.com/storage/object-storage/faq/buckets-objects/) — バージョニング・公開設定の挙動
- `.kiro/specs/b2-to-hetzner-migration/design.md` — 既存 Hetzner OS 認証情報チェーンの設計(再利用元)
