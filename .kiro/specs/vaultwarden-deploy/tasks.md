# Implementation Tasks — Vaultwarden Deploy

## Task 1: Infisical シークレット事前登録と検証

- [x] 1.1 Infisical に Vaultwarden 用シークレットを登録する
  - `VAULTWARDEN_ADMIN_TOKEN`、`VAULTWARDEN_DB_PASSWORD`、`VAULTWARDEN_ORG_CREATION_USERS`、`VAULTWARDEN_OIDC_CLIENT_ID`、`VAULTWARDEN_OIDC_CLIENT_SECRET` を Infisical に登録する
  - 登録後、`infisical secrets get` で各キーが取得できることを確認する
  - _Requirements: 4.1, 5.3, 6.5_
  - _Boundary: Secret Layer_

## Task 2: Terraform インフラ設定（DNS・Tunnel・Authentik）

- [x] 2.1 Cloudflare DNS と Tunnel 設定を追加する (P)
  - `dns.tf` に `vault.aramakisai.com` の CNAME レコードを追加する
  - `tunnel.tf` に `vault.aramakisai.com` → `http://vaultwarden.prod.svc.cluster.local:80` の ingress_rule を追加する
  - `terraform plan` で差分が意図通りのみであることを確認する
  - _Requirements: 3.1, 3.2, 3.3_
  - _Boundary: Infra Layer_

- [x] 2.2 Authentik OIDC Provider と Application を追加する (P)
  - `authentik_apps.tf` に Vaultwarden 用 `authentik_provider_oauth2` を追加する（client_id: `vaultwarden`、redirect URI 設定）
  - `authentik_application` で Vaultwarden アプリを登録する
  - `property_mappings` に `openid`, `email`, `profile`, `groups` を含める
  - `terraform plan` で差分を確認する
  - _Requirements: 6.1, 6.2, 6.4_
  - _Boundary: Infra Layer, vaultwarden-authentik_

## Task 3: CloudNativePG データベースクラスター構築

- [x] 3.1 Vaultwarden 用 PostgreSQL クラスターを定義する
  - `db-cluster.yaml` を作成する（`imageName: ghcr.io/cloudnative-pg/postgresql:16.8`、`skipEmptyWalArchiveCheck: enabled`、`instances: 1`、`storage: 5Gi`）
  - `barmanObjectStore` で Hetzner Object Storage へのバックアップを設定する（`retentionPolicy: "14d"`）
  - `vaultwarden-db-credentials` ExternalSecret を CNPG 形式で定義する
  - _Requirements: 2.1, 2.3, 4.1_
  - _Boundary: Data Layer, vaultwarden-db, vaultwarden-db-credentials_

- [x] 3.2 CNPG ScheduledBackup を定義する
  - `scheduled-backup.yaml` を作成し、日次フルバックアップを設定する
  - _Requirements: 2.3_
  - _Boundary: Data Layer, vaultwarden-db_

## Task 4: 永続ストレージとバックアップ設定

- [x] 4.1 PVC を定義する
  - `pvc.yaml` を作成する（`ReadWriteOnce`、`local-path`、`5Gi`、`vaultwarden-data`）
  - _Requirements: 2.2_
  - _Boundary: Storage Layer, vaultwarden-data_

- [x] 4.2 VolSync ReplicationSource を定義する
  - `replication-source.yaml` を作成する（`trigger.schedule: "0 */6 * * *"`、`restic mover`、`copyMethod: Direct`）
  - `retain.hourly: 12`、`retain.daily: 7` を設定する
  - `vaultwarden-restic-secret` を参照する
  - _Requirements: 2.4_
  - _Boundary: Storage Layer, vaultwarden-backup_

## Task 5: Vaultwarden アプリケーション層

- [x] 5.1 ExternalSecret を定義する
  - `external-secret.yaml` を作成する（`vaultwarden-secrets`、Infisical から `ADMIN_TOKEN`、`SMTP_PASSWORD`、`ORG_CREATION_USERS`、`SSO_CLIENT_ID`、`SSO_CLIENT_SECRET` を同期）
  - `refreshInterval: 1h`、`ClusterSecretStore: infisical` を設定する
  - _Requirements: 4.1, 4.2, 5.3, 6.5_
  - _Boundary: Secret Layer, vaultwarden-secrets_

- [x] 5.2 Deployment を定義する (P)
  - `deployment.yaml` を作成する（`replicas: 1`、イメージ `vaultwarden/server` に固定タグを推奨）
  - `DATABASE_URL`、`DATA_FOLDER=/data`、`WEBSOCKET_ENABLED=true`、`DOMAIN=https://vault.aramakisai.com`、`SIGNUPS_ALLOWED=false`、`SIGNUPS_DOMAINS_WHITELIST=""`、`SSO_ENABLED=true`、`SSO_AUTHORITY`、`SSO_ONLY=true`、`SSO_SIGNUPS_MATCH_EMAIL=true`、`SSO_PKCE=true` などの環境変数を設定する
  - `envFrom.secretRef` で `vaultwarden-secrets` を参照する
  - `livenessProbe` / `readinessProbe` で `/alive` をチェックする
  - `volumeMount` で `vaultwarden-data` PVC を `/data` にマウントする
  - _Requirements: 1.1-1.4, 2.1, 2.2, 3.1-3.3, 4.2, 5.1, 5.2, 6.1-6.5_
  - _Boundary: Backend Layer, vaultwarden-app_

- [x] 5.3 Service を定義する (P)
  - `service.yaml` を作成する（`ClusterIP`、port 80、selector `app: vaultwarden`）
  - _Requirements: 3.1_
  - _Boundary: Network Layer, vaultwarden-service_

## Task 6: ArgoCD Application と GitOps 統合

- [x] 6.1 ArgoCD Application 定義を作成する
  - `gitops/apps/prod/vaultwarden.yaml` を作成する（`sync-wave: "0"`）
  - `source.path` で `gitops/manifests/prod/vaultwarden/` を参照する
  - _Requirements: 2.1, 2.2, 3.1, 4.1, 4.2_
  - _Boundary: GitOps Layer_

## Task 7: 統合と検証

- [x] 7.1 Terraform 適用と DNS・Tunnel 検証
  - `terraform apply` を実行する
  - `vault.aramakisai.com` の DNS 解決と Tunnel 経由の疎通を確認する
  - _Depends: 2.1, 2.2_
  - _Requirements: 3.1, 3.2, 3.3_

- [ ] 7.2 ArgoCD Sync とアプリケーションデプロイ検証
  - ArgoCD 上で Vaultwarden Application が `Healthy` / `Synced` になることを確認する
  - `vaultwarden-db` Pod が Ready になってから `vaultwarden-app` Pod が起動することを確認する
  - `vaultwarden-secrets` Secret が ESO によって正しく作成されていることを確認する
  - `vaultwarden-data` PVC が Bound であることを確認する
  - _Depends: 3.1, 3.2, 4.1, 4.2, 5.1, 5.2, 5.3, 6.1_
  - _Requirements: 2.1, 2.2, 4.1, 4.2_

- [ ] 7.3 アクセス制御と SSO 連携検証
  - `https://vault.aramakisai.com` にアクセスし、HTTPS と WebSocket が機能することを確認する
  - `/admin` にアクセスし、`ADMIN_TOKEN` なしではアクセスできないことを確認する
  - `SIGNUPS_ALLOWED=false` 時に `/api/accounts/register` が拒否されることを確認する
  - SSO ボタンをクリックし、Authentik 認証後に Vaultwarden に自動ログインできることを確認する
  - `SSO_ONLY=true` 時にパスワードログインフォームが非表示であることを確認する
  - _Depends: 7.2_
  - _Requirements: 1.1-1.4, 5.1, 5.2, 5.3, 6.1-6.5_

- [ ] 7.4 バックアップ動作確認
  - CNPG ScheduledBackup が実行され、Hetzner Object Storage へバックアップが作成されることを確認する
  - VolSync ReplicationSource が 6 時間毎に実行され、Backblaze B2 へバックアップが作成されることを確認する
  - _Depends: 3.2, 4.2, 7.2_
  - _Requirements: 2.3, 2.4_
