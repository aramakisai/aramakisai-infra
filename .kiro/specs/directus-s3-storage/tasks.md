# Implementation Plan

- [x] 1. (P) Hetzner OS バケットでバージョニングとライフサイクルルールを設定する
- [x] 1.1 バージョニングを有効化する
  - `mc`(または AWS CLI) で `aramakisai-backups` バケットのオブジェクトバージョニングを有効化する
  - `mc stat <alias>/aramakisai-backups` で `Versioning: Enabled` と表示されること
  - _Requirements: 2.1_
  - _Boundary: Hetzner OS バケット設定_

- [x] 1.2 ライフサイクルルール (非現行バージョン30日後消去) を適用する
  - `NoncurrentVersionExpiration.NoncurrentDays: 30` を指定したルール定義 (`Prefix: ""`) を `mc ilm rule import` (または `aws s3api put-bucket-lifecycle-configuration`) で適用する
  - バージョニングが `Enabled` であることを適用前に確認する (サスペンド状態ではライフサイクルが機能しないため)
  - `mc ilm rule ls <alias>/aramakisai-backups` でルール `noncurrent-expiry` が登録されていること
  - _Requirements: 2.3_
  - _Boundary: Hetzner OS バケット設定_

- [x] 2. (P) directus-secrets に S3 認証情報を追加し main へ反映する
  - `gitops/manifests/prod/directus/external-secret.yaml` の `directus-secrets` ExternalSecret に `STORAGE_S3_KEY` (← `HETZNER_OS_ACCESS_KEY_ID`) / `STORAGE_S3_SECRET` (← `HETZNER_OS_SECRET_ACCESS_KEY`) を追加する (コミット1、新規 Infisical キー登録は不要)
  - 変更を main へ直接 push し ArgoCD sync を待つ
  - `make kubectl ARGS="get secret directus-secrets -n prod -o jsonpath='{.data.STORAGE_S3_KEY}'"` が空でない値を返すこと
  - _Requirements: 1.1_
  - _Boundary: directus-secrets ExternalSecret_

- [x] 3. (P) DR ランブックと復旧スクリプトの整合性を確認・更新する
- [x] 3.1 (P) recovery.sh に Directus PVC 復旧処理が存在しないことを確認する
  - `.github/scripts/recovery.sh` を確認し、Directus の PVC/VolSync リストア処理が含まれていないことを確認する (確認のみ、コード変更なし)
  - 確認結果 (処理が存在しないこと) を明文化できる状態になること
  - _Requirements: 4.1_
  - _Boundary: DR ランブック更新_

- [x] 3.2 (P) dr-runbook.md の Directus RPO/RTO 記載を更新する
  - `docs/dr-runbook.md` の RPO/RTO 表における Directus 行に、アセットは Hetzner OS への直接保存のため復元待機が不要であることを追記する
  - `docs/dr-runbook.md` にアセット復元待機に関する記載が含まれていないこと
  - _Requirements: 4.2_
  - _Boundary: DR ランブック更新_

- [x] 4. 既存 PVC アセットを Hetzner OS へ移行する手順を作成し実行する
  - **確認結果 (2026-06-17)**: `kubectl exec -n prod deploy/directus -- find /directus/uploads -type f | wc -l` = `0` (`du -sh` も `4.0K` = 空ディレクトリのみ)。本番運用開始前の初期状態であり既存アセットが一切存在しないため、移行作業 (4.1/4.2) は不要と判断しスキップする
- [x] 4.1 (P) 既存アセット移行手順書を作成する
  - **スキップ**: PVC 内アセットが 0 件のため転送対象が存在せず、`docs/directus-s3-migration.md` の作成は不要と判断
  - _Requirements: 3.1_
  - _Boundary: 既存アセット移行手順_

- [x] 4.2 既存 PVC アセットを Hetzner OS へ転送し照合を完了する
  - **スキップ**: 4.1 と同理由 (転送対象アセットが存在しない)。照合も 0 件 = 0 件で自明に一致
  - _Depends: 4.1, 1.2, 2_
  - _Requirements: 3.1_
  - _Boundary: 既存アセット移行手順_

- [x] 4.3 (P) terraform/storage.tf のコメントに Directus アセットの用途を追記する
  - `terraform/storage.tf` のバケット用途コメントに「Directus アセット (`directus-uploads/`)」を追記する (IaC リソース定義は変更しない)
  - コメントに CNPG WAL アーカイブ・VolSync バックアップ・Directus アセットの3用途が記載されていること
  - _Requirements: 1.1_
  - _Boundary: terraform/storage.tf_

- [x] 5. Directus のストレージ設定を S3 に切替え PVC を削除する
  - _Depends: 4.2_
  - `gitops/manifests/prod/directus/deployment.yaml` を編集する (コミット2、アトミックに適用)
    - `STORAGE_LOCATIONS=s3`、`STORAGE_S3_DRIVER=s3`、`STORAGE_S3_BUCKET=aramakisai-backups`、`STORAGE_S3_ROOT=directus-uploads`、`STORAGE_S3_ENDPOINT=https://fsn1.your-objectstorage.com`、`STORAGE_S3_REGION=fsn1`、`STORAGE_S3_FORCE_PATH_STYLE=false` を設定する
    - `uploads` の `volumeMounts`/`volumes` 定義と `directus-uploads` PVC リソース定義を削除する
  - 変更を main へ直接 push し ArgoCD sync を待つ
  - Directus Pod が `Ready` になり、`make kubectl ARGS="get pvc directus-uploads -n prod"` が `NotFound` を返すこと
  - _Requirements: 1.1, 1.3_
  - **実施結果 (2026-06-17)**: commit `3719806` を main へ push。ArgoCD は `argocd.argoproj.io/refresh: hard` annotation で強制リフレッシュするまで OutOfSync を検出しなかった (push 直後は古い revision を Synced と誤表示)。Hard refresh 後 `OutOfSync`→自動 sync で新 Pod `directus-549d689978-x6b89` が `1/1 Running`、`directus-uploads` PVC は `NotFound`、env に `STORAGE_LOCATIONS=s3` 含む `STORAGE_S3_*` 一式を確認。Application は `Synced`/`Healthy`

- [x] 6. S3 移行後の動作を実機確認する
  - _Depends: 5_
  - **実施方針 (2026-06-17)**: ホームページ側 (スキーマ/フロントエンド) が未着手のため、管理画面 UI ではなく `kubectl port-forward` + Directus REST API (`/auth/login`, `/files`, `/assets/:id`) + `aws s3api` による簡易確認に変更。検証範囲は変わらず (1.2, 2.1, 2.2 を満たす)
- [x] 6.1 新規アセットのアップロードと公開 URL 閲覧を確認する
  - `POST /files` でテストアセットをアップロード → レスポンス `"storage":"s3"` を確認
  - `aws s3api head-object --bucket aramakisai-backups --key directus-uploads/<file-id>.txt` でオブジェクト存在・`VersionId` 付与を確認 (`Expiration` ヘッダーで `noncurrent-expiry` ルール適用も確認)
  - `GET /assets/<file-id>` (port-forward 経由) でダウンロードし、元ファイルと `diff` 一致を確認
  - _Requirements: 1.2_

- [x] 6.2 削除マーカー付与とバージョン保持を確認する
  - 6.1 のテストアセットを `DELETE /files/<file-id>` で削除 (204)
  - `aws s3api list-object-versions` で旧バージョン (`IsLatest: false`, 元の `VersionId` 保持) と削除マーカー (`IsLatest: true`) が両方残っていることを確認
  - _Requirements: 2.1, 2.2_
