# 要件定義 (Requirements) - B2 から Hetzner Object Storage への移行

## はじめに

Backblaze B2 の Class B トランザクション上限超過問題の恒久対策として、オブジェクトストレージを Hetzner Object Storage (S3 互換) に一本化する。
本移行は 0→1 フェーズであるため、既存 B2 バックアップデータの転送は不要。
対象は CNPG WAL アーカイブ (authentik-db / directus-db)、VolSync restic バックアップ (mailserver)、および関連 ESO ExternalSecret 全体。

## スコープ

- **対象 (In scope)**:
  - Hetzner Object Storage バケットの準備 (手動作成)
  - Infisical シークレット更新 (B2 キー → Hetzner キー)
  - ESO ExternalSecret の差し替え (`b2-credentials` → `hetzner-os-credentials`)
  - CNPG db-cluster (authentik-db / directus-db) のバックアップ先変更 + bootstrap モード変更
  - VolSync ReplicationSource (mailserver) のリポジトリ URL 変更
  - `backup` spec の内容更新 (B2 前提の記述を Hetzner に更新)
  - terraform/storage.tf のコメント整備

- **対象外 (Out of scope)**:
  - B2 から Hetzner への既存バックアップデータ転送 (0→1 フェーズのため不要)
  - VolSync ReplicationDestination を用いたリストア検証 (DR spec で扱う)
  - Hetzner Object Storage バケットの Terraform 管理化 (provider が未サポートのため手動運用を継続)
  - Redis・Roundcube PVC のバックアップ追加 (既存の backup spec と同様に対象外)

- **隣接 spec との関係**:
  - `backup` spec: バックアップ先が B2 前提で記述されているため本移行完了後に更新が必要
  - `dr-automation` spec: DR フローは Hetzner 移行後も基本的に変わらないが、リストア手順の URL が変わる

## 要件

### 要件 1: Hetzner Object Storage バケットの準備

**目的:** インフラ担当者として、Hetzner Object Storage にバックアップ先バケットを用意したい。B2 トランザクション超過を防ぎながら同一クラウドでデータを保管できるようにするため。

#### 受け入れ基準

1. The インフラ担当者 shall Hetzner Robot ダッシュボードで `aramakisai-backups` バケットを手動作成し、リージョン (`fsn1` または `nbg1`) を選択すること
2. When バケット作成が完了したとき、the インフラ担当者 shall アクセスキーペア (Access Key ID / Secret Access Key) を発行し、Infisical の prod 環境に `HETZNER_OS_ACCESS_KEY_ID` / `HETZNER_OS_SECRET_ACCESS_KEY` として登録すること
3. The インフラ担当者 shall Hetzner Object Storage のエンドポイント URL (`https://<location>.your-objectstorage.com`) を確認し、本 spec の設計フェーズに記録すること
4. If バケット名 `aramakisai-backups` が既に使用中のとき、the インフラ担当者 shall 別の名前を選択し関連マニフェストに反映すること

### 要件 2: Infisical シークレット管理の更新

**目的:** インフラ担当者として、Infisical に登録されたシークレットを B2 から Hetzner に切り替えたい。クラスター内の全コンポーネントが新しい認証情報を参照できるようにするため。

#### 受け入れ基準

1. When Hetzner アクセスキーが Infisical に登録されたとき、the インフラ担当者 shall 旧 B2 キー (`B2_KEY_ID` / `B2_APPLICATION_KEY`) を Infisical から削除または無効化すること
2. The Infisical prod 環境 shall `HETZNER_OS_ACCESS_KEY_ID` と `HETZNER_OS_SECRET_ACCESS_KEY` の 2 つのキーを保持すること
3. If 移行期間中に旧 B2 キーが必要になるとき、the インフラ担当者 shall 新旧キーを並存させたうえで移行完了後に旧キーを削除すること

### 要件 3: ESO ExternalSecret の差し替え

**目的:** インフラ担当者として、クラスター内の認証情報 Secret を Hetzner 用に切り替えたい。CNPG と VolSync が同一の Kubernetes Secret から新しい認証情報を取得できるようにするため。

#### 受け入れ基準

1. The GitOps リポジトリ shall `gitops/manifests/shared/eso/b2-external-secret.yaml` を Hetzner 用の ExternalSecret (`hetzner-os-credentials`) に置き換えること
2. The 新 ExternalSecret shall `ACCESS_KEY_ID` / `SECRET_ACCESS_KEY` のキー名で `hetzner-os-credentials` Secret を `prod` namespace に作成すること (CNPG barman との互換性維持)
3. When ArgoCD が新 ExternalSecret を sync したとき、the ESO shall Infisical から `HETZNER_OS_ACCESS_KEY_ID` / `HETZNER_OS_SECRET_ACCESS_KEY` を取得して Secret を更新すること
4. The VolSync restic 用 ExternalSecret (`mailserver-restic-secret`) shall `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` のキーを `HETZNER_OS_ACCESS_KEY_ID` / `HETZNER_OS_SECRET_ACCESS_KEY` から取得するよう更新すること

### 要件 4: CNPG db-cluster のバックアップ先変更

**目的:** インフラ担当者として、authentik-db / directus-db の WAL アーカイブと定期バックアップ先を Hetzner Object Storage に変更したい。0→1 フェーズのためデータ損失なく新しいストレージにバックアップを蓄積し始められるようにするため。

#### 受け入れ基準

1. The `authentik-db` および `directus-db` CNPG Cluster shall `backup.barmanObjectStore.endpointURL` を `https://s3.us-west-004.backblazeb2.com` から Hetzner Object Storage エンドポイント URL に変更すること
2. The CNPG Cluster shall `backup.barmanObjectStore.s3Credentials` の参照先を `b2-credentials` から `hetzner-os-credentials` に変更すること
3. While 0→1 フェーズにつき Hetzner にリストア元バックアップが存在しないとき、the CNPG Cluster shall `bootstrap.recovery` を `bootstrap.initdb` に変更すること (空の S3 パスからの復元を防ぐため)
4. When `bootstrap.initdb` に変更したとき、the GitOps リポジトリ shall `externalClusters` セクションを削除または空にすること
5. The CNPG Cluster shall `backup.barmanObjectStore.destinationPath` を Hetzner バケット名に合わせて更新すること (例: `s3://aramakisai-backups/cnpg/authentik-db`)
6. If WAL アーカイブが正常に機能しているとき、the CNPG Cluster shall WAL ファイルを Hetzner Object Storage に書き込めること (初回バックアップ後に確認可能)

### 要件 5: VolSync (mailserver) のリポジトリ変更

**目的:** インフラ担当者として、mailserver-data PVC の restic バックアップ先を Hetzner Object Storage に変更したい。B2 のトランザクション超過を回避しながらメールデータを継続的に保護するため。

#### 受け入れ基準

1. The `mailserver-restic-secret` ExternalSecret shall `RESTIC_REPOSITORY` を Hetzner Object Storage S3 URL (`s3:https://<endpoint>/<bucket>/mailserver/restic`) に変更すること
2. The `mailserver-restic-secret` ExternalSecret shall `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` を Hetzner の認証情報から取得するよう更新すること
3. The VolSync `ReplicationSource` (mailserver-backup) shall バックアップスケジュール (`0 */6 * * *`) および保持ポリシーを変更せず継続すること
4. When ArgoCD が更新後のマニフェストを sync し、次のスケジュールサイクルが到来したとき、the VolSync restic mover shall Hetzner Object Storage への書き込みに成功し `ReplicationSource.status.lastSyncTime` が更新されること
5. If restic リポジトリが Hetzner 上に存在しないとき、the VolSync restic mover shall 自動的に新規リポジトリを初期化すること (`restic init` を自動実行)

### 要件 6: terraform/storage.tf のコメント整備

**目的:** インフラ担当者として、storage.tf のコメントを実態に合わせて更新したい。Hetzner Object Storage の用途と運用方法が明確になるようにするため。

#### 受け入れ基準

1. The `terraform/storage.tf` shall コメントの「用途」を実際の用途 (CNPG WAL アーカイブ + VolSync restic バックアップ) に更新すること
2. The `terraform/storage.tf` shall エンドポイント URL を実際のリージョン URL に更新すること
3. The `terraform/storage.tf` shall バケットの手動作成が必要な理由 (provider 非サポート) を維持しつつ、Hetzner Robot での作成手順への参照を追記すること

### 要件 7: backup spec の更新

**目的:** インフラ担当者として、`backup` spec の要件定義を現在の構成 (Docker Mailserver + Hetzner OS) に合わせて更新したい。outdated な B2・Stalwart 前提の記述が混乱を招かないようにするため。

#### 受け入れ基準

1. The `backup` spec の `requirements.md` shall Backblaze B2 の参照を Hetzner Object Storage に置き換えること
2. The `backup` spec の `requirements.md` shall "Stalwart" の参照を "Docker Mailserver (DMS)" に置き換えること
3. The `backup` spec shall 要件 1 (B2 セットアップ) を Hetzner Object Storage のセットアップ要件に更新すること
4. When backup spec の更新が完了したとき、the インフラ担当者 shall `spec.json` の `updated_at` タイムスタンプを更新すること
