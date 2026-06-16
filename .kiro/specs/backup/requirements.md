# 要件定義 (Requirements) - バックアップ

## 1. 目的

現状バックアップが存在せず、ノード障害時にすべてのデータが失われるリスクがある。
Hetzner Object Storage (S3互換) を共通のバックアップ先として、PostgreSQL と Docker Mailserver (DMS) メールデータを定期的に保護する。

## 2. 現状と前提条件

- **Hetzner Object Storage バケット**: Hetzner Robot コンソールで手動作成が必要 (`console.hetzner.com`)
- **エンドポイント**: `https://fsn1.your-objectstorage.com`
- **バケット名**: `aramakisai-backups`
- **CloudNativePG**: Authentik DB / Directus DB ともに `instances: 1`。バックアップ設定なし
- **Docker Mailserver (DMS)**: `mailserver-data` PVC (prod-node-1 固定)。バックアップなし
- **ESO**: `ClusterSecretStore` (infisical) が稼働済みのため ExternalSecret で S3 認証情報を注入できる

## 3. 要件

### 要件 1: Hetzner Object Storage のセットアップ
- Hetzner Robot コンソール (`console.hetzner.com`) で `aramakisai-backups` バケットを `fsn1` リージョンに手動作成する
- アクセスキーペア (Access Key ID / Secret Access Key) を発行し、Infisical に `HETZNER_OS_ACCESS_KEY_ID` / `HETZNER_OS_SECRET_ACCESS_KEY` として登録する
- `gitops/manifests/shared/eso/` に `hetzner-os-credentials` ExternalSecret を追加し、クラスター内で Secret として利用できるようにする

### 要件 2: CloudNativePG の定期バックアップ
- Authentik DB (`authentik-db`) と Directus DB (`directus-db`) それぞれに Hetzner Object Storage への WAL アーカイブを設定する
- `ScheduledBackup` リソースで毎日 02:00 UTC にフルバックアップを取得する
- 保持期間: 7日間 (`retentionPolicy: 7d`)
- Point-in-Time Recovery (PITR) が可能な状態を維持する

### 要件 3: Docker Mailserver (DMS) メールデータの定期バックアップ
- VolSync + restic を使用して DMS メールデータ PVC を Hetzner Object Storage に定期バックアップする
- スケジュール: **6時間ごと** (`0 */6 * * *`)
- 保持ポリシー: 直近スナップショット + 日次 7日間
- バックアップが正常完了したことを `ReplicationSource` の status で確認できること

### 要件 4: ノード全損時のフル DR 手順
- 全ノードを再作成（terraform apply → ansible-playbook）した直後の空クラスターから、すべてのサービスがデータ込みで復旧できること
- 復旧の順序・コマンドを手順書レベルで tasks に明記し、担当者が手順書を見ながら実行できること
- 復旧完了の定義: Roundcube でメールの送受信が確認できる状態（DMS IMAP/SMTP + Authentik 認証 + Directus CMS が正常稼働）
- RTO (目標復旧時間) の目安を各ステップで示すこと

## 4. スコープ外

- Roundcube PVC (`roundcube-db`, 1Gi / SQLite): メール本文は DMS 側にあるため優先度低
- Redis: 揮発性キャッシュ。再起動で再生成されるためバックアップ不要
- リストア手順の自動化: 手動リストア手順の文書化のみ (自動化は別スペック)
- Longhorn 等の分散ストレージ導入: HA 改善は別スペック (`ha-improvement`) で扱う
- 二次バックアップ (Google Drive 等): Hetzner OS 単体で十分と判断し対象外
