# 基本設計 (Design) - バックアップ

## 1. 全体構成

```
クラスター
  CNPG 02:00 ────┐
  VolSync */2h ──┤  Backblaze B2 (aramakisai-backups)
                 │  ├── cnpg/authentik-db/  (WAL + full)
                 │  ├── cnpg/directus-db/   (WAL + full)
                 └── volsync/stalwart/      (restic repo, 暗号化)

クラスター内の認証フロー:
  Infisical → ESO ExternalSecret → b2-credentials Secret
    ├── CloudNativePG Cluster.backup.barmanObjectStore が参照
    └── VolSync ReplicationSource が参照
```

## 2. コンポーネント詳細

### 2.1. S3 認証情報 (shared)

`gitops/manifests/shared/eso/b2-external-secret.yaml` を新規作成し、
`ClusterSecretStore: infisical` 経由で Backblaze B2 認証情報を取得する。

Backblaze B2 の Application Key ID / Application Key は S3 互換 API では
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` として扱う。

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: b2-credentials
  namespace: prod          # CNPG と VolSync が同じ namespace を参照
spec:
  secretStoreRef:
    kind: ClusterSecretStore
    name: infisical
  target:
    name: b2-credentials
  data:
    - secretKey: ACCESS_KEY_ID
      remoteRef:
        key: B2_KEY_ID
    - secretKey: SECRET_ACCESS_KEY
      remoteRef:
        key: B2_APPLICATION_KEY
```

### 2.2. CloudNativePG バックアップ設定

#### db-cluster.yaml への追加 (Authentik / Directus 共通パターン)

```yaml
spec:
  backup:
    barmanObjectStore:
      destinationPath: "s3://aramakisai-backups/cnpg/<cluster-name>"
      endpointURL: "https://s3.us-west-004.backblazeb2.com"
      s3Credentials:
        accessKeyId:
          name: b2-credentials
          key: ACCESS_KEY_ID
        secretAccessKey:
          name: b2-credentials
          key: SECRET_ACCESS_KEY
      wal:
        compression: gzip
      data:
        compression: gzip
    retentionPolicy: "7d"
```

#### ScheduledBackup リソース

`gitops/manifests/prod/authentik/scheduled-backup.yaml` と
`gitops/manifests/prod/directus/scheduled-backup.yaml` を新規作成。

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: ScheduledBackup
metadata:
  name: <cluster-name>-daily
  namespace: prod
spec:
  schedule: "0 2 * * *"      # 毎日 02:00 UTC
  backupOwnerReference: self
  cluster:
    name: <cluster-name>
  target: prefer-standby      # Primary への負荷を避ける
```

### 2.3. VolSync による Stalwart PVC バックアップ

**VolSync** を ArgoCD で管理し、`stalwart-data` PVC を restic 経由で B2 にバックアップする。
スケジュールは **2時間ごと**とし、最大メール消失を 2 時間以内に抑える。

#### ArgoCD Application

`gitops/apps/prod/volsync.yaml` を新規作成 (wave: -1)。

#### ReplicationSource

`gitops/manifests/prod/stalwart/replication-source.yaml` を新規作成。

```yaml
apiVersion: volsync.backube/v1alpha1
kind: ReplicationSource
metadata:
  name: stalwart-backup
  namespace: prod
spec:
  sourcePVC: stalwart-data
  trigger:
    schedule: "0 */2 * * *"  # 2時間ごと (最大メール消失 2 時間)
  restic:
    pruneIntervalDays: 1
    repository: stalwart-restic-secret   # restic リポジトリ設定
    retain:
      hourly: 12    # 直近 24 時間分 (2h × 12)
      daily: 7      # 日次スナップショット 7 日分
    copyMethod: Direct
```

#### restic Secret (ExternalSecret)

`gitops/manifests/prod/stalwart/restic-external-secret.yaml` を新規作成。
restic が必要とする環境変数 (`RESTIC_REPOSITORY`, `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `RESTIC_PASSWORD`) を Infisical から注入する。

Infisical に追加するキー:
- `STALWART_RESTIC_REPOSITORY` = `s3:https://s3.us-west-004.backblazeb2.com/aramakisai-backups/volsync/stalwart`
- `STALWART_RESTIC_PASSWORD` = ランダム生成パスフレーズ

## 3. リストア手順 (参考)

### CloudNativePG PITR

```bash
# クラスタを recovery モードで再作成
kubectl apply -f - <<EOF
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: authentik-db-recovery
  namespace: prod
spec:
  instances: 1
  bootstrap:
    recovery:
      source: authentik-db
      recoveryTarget:
        targetTime: "2026-06-01 10:00:00"
  externalClusters:
    - name: authentik-db
      barmanObjectStore:
        destinationPath: s3://aramakisai-backups/cnpg/authentik-db
        endpointURL: https://s3.us-west-004.backblazeb2.com
        s3Credentials:
          accessKeyId:
            name: b2-credentials
            key: ACCESS_KEY_ID
          secretAccessKey:
            name: b2-credentials
            key: SECRET_ACCESS_KEY
EOF
```

### VolSync リストア

```bash
# ReplicationDestination を作成して PVC にリストア
kubectl apply -f - <<EOF
apiVersion: volsync.backube/v1alpha1
kind: ReplicationDestination
metadata:
  name: stalwart-restore
  namespace: prod
spec:
  trigger:
    manual: restore-$(date +%Y%m%d)
  restic:
    repository: stalwart-restic-secret
    destinationPVC: stalwart-data
    copyMethod: Snapshot
EOF
```

## 4. ノード全損時のフル DR 設計

### 前提：何が消えて、何が残るか

| データ | 消える? | 理由 | 復旧元 |
|--------|---------|------|--------|
| K3s etcd (クラスター状態) | ✅ 消える | local-path、ノードに紐づく | Git (ArgoCD re-sync) |
| Authentik DB | ✅ 消える | CNPG PVC が local-path | Backblaze B2 (barman PITR) |
| Directus DB | ✅ 消える | 同上 | Backblaze B2 (barman PITR) |
| Stalwart メールデータ | ✅ 消える | PVC が local-path + prod-node-1 固定 | Backblaze B2 (restic、最大 2h 前) |
| Roundcube SQLite | ✅ 消える | PVC が local-path | **復旧不可**（許容）|
| Cloudflare Tunnel Token | ✅ 消える (Secret) | Infisical が保持 | ESO が自動再作成 |
| TLS 証明書 | ✅ 消える (Secret) | cert-manager が再発行 | Let's Encrypt (自動) |
| Stalwart ACME 証明書 | ✅ 消える | Stalwart 内部保持 | Let's Encrypt DNS-01 (自動) |
| DNS レコード | ❌ 残る | Cloudflare 管理 | 不要 |
| Cloudflare Tunnel 設定 | ❌ 残る | Terraform 管理 | 不要 |
| Infisical シークレット | ❌ 残る | 外部サービス | 不要 |

### 復旧シーケンス

```
フェーズ 1: インフラ再構築 (~30分)
  terraform apply
    → Hetzner ノード再作成 + Tailscale 接続
  ansible-playbook k3s-bootstrap.yml
    → K3s + Cilium + cloudflared + ArgoCD + App of Apps

フェーズ 2: ESO / ArgoCD による自動復旧 (~10分)
  ArgoCD が Git から全マニフェストを sync
    → ESO が Infisical から全 Secret を再作成
    → cert-manager が TLS 証明書を再発行
    → CNPG Operator・VolSync が CRD を提供

  ※ この時点で CNPG Cluster と Stalwart StatefulSet は起動するが
    PVC が空のため DB 接続エラー / メールデータなしの状態

フェーズ 3: DB 復旧 (~20分, 並列実行可)
  Authentik DB: CNPG bootstrap.recovery で B2 から PITR リストア
  Directus DB:  同上

フェーズ 4: Stalwart メールデータ復旧 (~10〜30分、データ量による)
  Stalwart を停止 → VolSync ReplicationDestination でリストア → 再起動

フェーズ 5: 疎通確認 (~10分)
  Roundcube でログイン → メール送受信確認
```

**目標 RTO: 約 80〜100 分**

### DB 復旧時の重要な手順

CNPG のリストア時、既存の空クラスター (`authentik-db`) を**一度削除してから** recovery モードの新クラスターを作成する必要がある。

```yaml
# recovery 用の一時 Cluster 定義 (kubectl apply で直接投入)
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: authentik-db          # 既存と同名で上書き
  namespace: prod
spec:
  instances: 3
  bootstrap:
    recovery:
      source: authentik-db-backup
      recoveryTarget:
        targetTime: "YYYY-MM-DD HH:MM:SS"   # 最新で良い場合は省略可
  externalClusters:
    - name: authentik-db-backup
      barmanObjectStore:
        destinationPath: s3://aramakisai-backups/cnpg/authentik-db
        endpointURL: https://s3.us-west-004.backblazeb2.com
        s3Credentials:
          accessKeyId:
            name: b2-credentials
            key: ACCESS_KEY_ID
          secretAccessKey:
            name: b2-credentials
            key: SECRET_ACCESS_KEY
```

リストア完了後は ArgoCD の `db-cluster.yaml` (通常起動設定) で上書き sync する。

### Stalwart 復旧時の注意

- Stalwart は `nodeSelector: prod-node-1` が固定。Terraform でノード名を同じにして再作成すれば hostname も同じになるため nodeSelector の問題は発生しない
- PVC `stalwart-data` は ArgoCD sync で空の状態で作成される。ReplicationDestination でリストアする前に Stalwart Pod を停止しておくこと（PVC がマウントされたまま書き込むと破損リスク）

## 6. 検証方法

- CNPG: `kubectl get backup -n prod` でバックアップ一覧を確認。`status.phase: completed` を確認
- VolSync: `kubectl get replicationsource stalwart-backup -n prod -o jsonpath='{.status.lastSyncTime}'` で最終成功時刻を確認
- B2: Backblaze コンソールでオブジェクト数の増加を確認
- フル DR 検証: Task 8 のシナリオで staging ノードを使って実際に手順を通しで実行する
