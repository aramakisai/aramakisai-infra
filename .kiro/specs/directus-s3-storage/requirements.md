# Requirements Document - Directus S3 アセットストレージ移行

## 1. 導入背景 (Introduction)

現在、Directus のファイルアップロードデータ（アセット）はローカルの PVC (`directus-uploads` 10GiB) に保存されており、DR（災害復旧）発生時のバックアップ・復元経路が確立されていません。このままだと、クラスター再構築時にデータベース（メタデータ）は復旧しても、画像などのファイル実体がすべて失われるリスクがあります。

この課題を解決するため、アセットの保存先を PVC から直接 **Hetzner Object Storage (S3互換)** に切り替えます。さらに、誤操作によるファイル削除からデータを保護するために、**オブジェクトバージョニング**を有効化します。

これにより、DR復旧プロセスにおいて **「Directus アセットの PVC リストア手順を完全に排除」** し、インフラのステートレス化とDR時間の短縮を実現します。

---

## 2. 境界コンテキスト (Boundary Context)

- **In-Scope**:
  - Directus (`deployment.yaml`) の環境変数修正による S3 (B2) 保存への切り替え
  - [deployment.yaml](../../../gitops/manifests/prod/directus/deployment.yaml) 内の `directus-uploads` PVC 定義およびマウントの削除
  - Infisical への Directus 用 S3 認証情報の追加と、ExternalSecret の更新
  - 対象 Hetzner Object Storage バケットに対するオブジェクトバージョニングおよびライフサイクルルール（30日保持）の適用（手動設定）
  - 移行期における、既存 PVC 内アセットの Hetzner OS への移行手順の確立
- **Out-of-Scope**:
  - Authentik や Stalwart 等、他サービスのストレージ設定変更
  - バックアップ用 Hetzner OS バケット自体の新規作成（既存の `aramakisai-backups` バケット内の別プレフィックス、または既存環境を流用する）

---

## 3. 要件 (Requirements)

### REQ-01: Directus アセットの S3 (Hetzner OS) 保存化
- **概要**: Directus がアップロードされたファイルを PVC ではなく、直接 Hetzner Object Storage (S3互換) に書き込み・読み込みできるようにする。
- **アクセプタンス基準**:
  1. Directus の環境変数 `STORAGE_LOCATIONS` に `s3` が正しく構成されており、エンドポイントが `https://fsn1.your-objectstorage.com` を指していること。
  2. 管理画面から画像等のアセットを新規アップロードした際、ファイルの実体が Hetzner OS バケット（`s3://aramakisai-backups/directus-uploads/`）に直接保存され、公開URLから正常に閲覧できること。
  3. `deployment.yaml` から `directus-uploads` PVC の定義とコンテナへのマウントが完全に削除されていること。

### REQ-02: 誤削除防止のためのオブジェクトバージョニングとライフサイクル管理
- **概要**: ユーザーによるアセットの誤削除や改ざんからデータを保護するため、Hetzner Object Storage バケットでの履歴管理（バージョニング）とコスト抑制のためのライフサイクルを設定する。
- **アクセプタンス基準**:
  1. アセットが保存される Hetzner OS バケット（またはフォルダプレフィックス）で「オブジェクトバージョニング（Object Versioning）」が有効になっていること。
  2. Directus 画面上からファイルを削除した際、Hetzner OS 上では削除マーカーが付与されるのみで、古いバージョンのファイル実体が安全に保持されること。
  3. 非現行バージョン（削除・上書きされた過去のファイル）を30日間保持した後に自動で完全消去するライフサイクルルールが適用されていること。

### REQ-03: 既存ローカルアセットの移行手順の整備
- **概要**: 現在すでにローカル PVC 内に存在するファイルを、稼働停止時間を最小限に抑えつつ安全に Hetzner Object Storage へ転送・移行する手順を確立する。
- **アクセプタンス基準**:
  1. ローカル PVC 内の `/directus/uploads` のアセットを Hetzner OS へ安全にアップロードするための CLI 操作（rclone または aws-cli を使用）の手順書が作成されていること。

### REQ-04: DR 手順からの PVC リストア排除
- **概要**: アセットがS3化されたことに伴い、DR ランブックや復旧スクリプトから Directus PVC の復旧に関する手順が完全に不要化されていることを確認する。
- **アクセプタンス基準**:
  1. `recovery.sh` において Directus の PVC 復元（VolSync）に関する処理を追加する必要がないことを確認する。
  2. [dr-runbook.md](../../../docs/dr-runbook.md) の復旧手順にアセットの復元待機が含まれていないこと。

---

## 4. 目標値 (RTO / RPO)

| 対象データ | RPO (目標復旧時点) | RTO (目標復旧時間) | 備考 |
| :--- | :--- | :--- | :--- |
| **Directus DB** (メタデータ) | 直前まで (CNPG WAL 連続アーカイブ) | 30分 | Hetzner OS から自動リストア |
| **Directus アセット** (画像等) | 直前まで (Hetzner OS への直接リアルタイム保存) | **即時 (0分)** | リストア手順不要。コンテナ起動と同時に接続 |
