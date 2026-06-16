# Implementation Plan

- [ ] 1. Hetzner OS バケット作成と Infisical シークレット準備
- [x] 1.1 Hetzner Object Storage バケットを作成しエンドポイントを確認する
  - Hetzner Robot コンソール (`console.hetzner.com`) で `aramakisai-backups` バケットを `fsn1` リージョンに作成する
  - バケット作成後にエンドポイント URL `https://fsn1.your-objectstorage.com` を確認し記録する
  - バケット名が既に使用中の場合は代替名を選択し、本 spec 設計書の File Structure Plan のコメントを更新する
  - バケット一覧に `aramakisai-backups` が表示されること
  - _Requirements: 1.1, 1.3, 1.4_

- [ ] 1.2 Hetzner アクセスキーを発行して Infisical に登録する
  - Hetzner Robot の Object Storage 設定でアクセスキーペア (Access Key ID / Secret Access Key) を発行する
  - Infisical の prod 環境に `HETZNER_OS_ACCESS_KEY_ID` / `HETZNER_OS_SECRET_ACCESS_KEY` として登録する
  - Infisical の prod 環境に 2 つのキーが存在することを UI で確認すること
  - _Requirements: 1.2, 2.2_

- [ ] 1.3 VolSync restic 用リポジトリ URL を Hetzner 向けに更新する
  - Infisical prod 環境の `MAILSERVER_RESTIC_REPOSITORY` の値を `s3:https://fsn1.your-objectstorage.com/aramakisai-backups/mailserver/restic` に更新する (バケット名を変更した場合はそれに合わせる)
  - Infisical の `MAILSERVER_RESTIC_REPOSITORY` 値が Hetzner エンドポイントを指しており、`backblazeb2.com` を含まないこと
  - _Requirements: 5.1_

- [x] 2. ESO ExternalSecret を Hetzner 認証情報に差し替える
- [x] 2.1 (P) b2-credentials ExternalSecret を削除し hetzner-os-credentials を作成する
  - `gitops/manifests/shared/eso/b2-external-secret.yaml` を削除する
  - `gitops/manifests/shared/eso/hetzner-os-external-secret.yaml` を新規作成する
    - `metadata.namespace: prod`、`spec.target.name: hetzner-os-credentials`
    - `secretKey: ACCESS_KEY_ID` ← `remoteRef.key: HETZNER_OS_ACCESS_KEY_ID`
    - `secretKey: SECRET_ACCESS_KEY` ← `remoteRef.key: HETZNER_OS_SECRET_ACCESS_KEY`
    - `refreshInterval: 1h`、`creationPolicy: Owner`、`secretStoreRef: infisical` (既存パターンと統一)
  - `shared/eso/` ディレクトリに `b2-external-secret.yaml` が存在せず `hetzner-os-external-secret.yaml` が存在すること
  - _Requirements: 3.1, 3.2, 3.3_
  - _Boundary: ESO ExternalSecret Layer_

- [x] 2.2 (P) mailserver restic ExternalSecret の認証情報キー参照を更新する
  - `gitops/manifests/prod/mailserver/restic-external-secret.yaml` を編集する
    - `AWS_ACCESS_KEY_ID` の `remoteRef.key` を `B2_KEY_ID` → `HETZNER_OS_ACCESS_KEY_ID` に変更
    - `AWS_SECRET_ACCESS_KEY` の `remoteRef.key` を `B2_APPLICATION_KEY` → `HETZNER_OS_SECRET_ACCESS_KEY` に変更
  - ファイル内に `B2_KEY_ID` および `B2_APPLICATION_KEY` の文字列が残っていないこと
  - _Requirements: 3.4, 5.2_
  - _Boundary: ESO ExternalSecret Layer_

- [x] 3. CNPG db-cluster のバックアップ先と起動方式を更新する
- [x] 3.1 (P) authentik-db を Hetzner OS バックアップ + initdb 起動に変更する
  - `gitops/manifests/prod/authentik/db-cluster.yaml` を編集する
    - `bootstrap` セクション: `recovery` ブロック全体を `initdb: {}` に置き換える
    - `externalClusters` セクションを丸ごと削除する
    - `backup.barmanObjectStore.endpointURL`: `https://s3.us-west-004.backblazeb2.com` → `https://fsn1.your-objectstorage.com`
    - `backup.barmanObjectStore.s3Credentials.accessKeyId.name` および `secretAccessKey.name`: `b2-credentials` → `hetzner-os-credentials`
    - `cnpg.io/skipEmptyWalArchiveCheck: enabled` アノテーションと `imageName: ghcr.io/cloudnative-pg/postgresql:16.8` は維持する
  - ファイル内に `backblazeb2.com`、`b2-credentials`、`externalClusters`、`bootstrap.recovery` の文字列が残っていないこと
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - _Boundary: CNPG authentik-db Cluster_

- [x] 3.2 (P) directus-db を Hetzner OS バックアップ + initdb 起動に変更する
  - `gitops/manifests/prod/directus/db-cluster.yaml` を編集する (authentik-db と同一変更パターンを適用)
    - `bootstrap.recovery` → `bootstrap.initdb: {}`
    - `externalClusters` セクションを削除
    - `backup.barmanObjectStore.endpointURL` を `https://fsn1.your-objectstorage.com` に変更
    - `s3Credentials.accessKeyId.name` / `secretAccessKey.name` を `hetzner-os-credentials` に変更
    - `cnpg.io/skipEmptyWalArchiveCheck: enabled` と `imageName: ghcr.io/cloudnative-pg/postgresql:16.8` を維持
  - ファイル内に `backblazeb2.com`、`b2-credentials`、`externalClusters`、`bootstrap.recovery` の文字列が残っていないこと
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - _Boundary: CNPG directus-db Cluster_

- [x] 4. 補足設定と関連ドキュメントを更新する
- [x] 4.1 (P) terraform/storage.tf のコメントを実態に合わせて更新する
  - `terraform/storage.tf` を編集する
    - 「用途」を "CNPG WAL アーカイブ + VolSync restic バックアップ" に更新する
    - エンドポイントを `https://fsn1.your-objectstorage.com` に更新する
    - バケット手動作成が必要な理由 (provider 非サポート) のコメントを維持し、Hetzner Robot での作成を明記する
  - コメントアウト中の `resource "hcloud_object_storage_bucket"` ブロックは変更しないこと
  - ファイル内に `Velero` / `Longhorn` の用途記述が残っていないこと
  - _Requirements: 6.1, 6.2, 6.3_
  - _Boundary: terraform/storage.tf_

- [x] 4.2 (P) backup spec の要件定義を現在の構成に更新する
  - `.kiro/specs/backup/requirements.md` を編集する
    - Backblaze B2 の参照 (エンドポイント URL・バケット設定手順など) をすべて Hetzner Object Storage に書き換える
    - "Stalwart" の参照をすべて "Docker Mailserver (DMS)" に書き換える
    - 要件 1 「バックアップ先のセットアップ」をHetzner Object Storage の手動作成手順に更新する
  - `.kiro/specs/backup/spec.json` の `updated_at` を現在のタイムスタンプに更新する
  - `backup/requirements.md` に `backblazeb2.com`、`Stalwart` の文字列が残っていないこと
  - _Requirements: 7.1, 7.2, 7.3, 7.4_
  - _Boundary: .kiro/specs/backup/_

- [ ] 5. ArgoCD sync 後の動作確認
- [ ] 5.1 ESO Secret 状態と CNPG クラスターの起動を確認する
  - タスク 2〜4 の変更を 1 つの PR にまとめ、タスク 1 の Infisical 準備完了後にマージする
  - `make kubectl ARGS="get secret hetzner-os-credentials -n prod"` で Secret の存在を確認する
  - `make kubectl ARGS="get secret b2-credentials -n prod"` で Secret が削除済み (NotFound) であることを確認する
  - `make kubectl ARGS="describe cluster authentik-db -n prod"` と `make kubectl ARGS="describe cluster directus-db -n prod"` で Phase が `Cluster in healthy state` になることを確認する
  - 両 CNPG クラスターが `Ready` 状態になり WAL アーカイブが Hetzner OS バケットに書き込まれること
  - _Requirements: 3.3, 4.6_

- [ ] 5.2 VolSync 次バックアップサイクルの Hetzner OS への書き込みを確認する
  - 次のスケジュール (`0 */6 * * *`) が到来するまで待機する (最大 6 時間)
  - `make kubectl ARGS="get replicationsource mailserver-backup -n prod -o jsonpath='{.status}'"` で `lastSyncTime` が更新されていることを確認する
  - Hetzner OS バケット内に `mailserver/restic/` パス以下の restic オブジェクト (config, index/ 等) が作成されること
  - _Requirements: 5.3, 5.4, 5.5_

- [ ] 6. B2 orphan シークレットをクリーンアップする
- [ ] 6.1 Infisical と Backblaze B2 コンソールから旧認証情報を削除する
  - タスク 5 の全確認完了後に実施する
  - Infisical の prod 環境から `B2_KEY_ID` / `B2_APPLICATION_KEY` を削除する
  - Backblaze B2 コンソール (`secure.backblaze.com`) で当該 Application Key を失効・削除する
  - Infisical に `B2_KEY_ID` / `B2_APPLICATION_KEY` が存在しないこと、かつクラスターの全サービスが正常稼働していること
  - _Requirements: 2.1, 2.3_
