# タスク定義 (Tasks) - バックアップ

## タスク一覧

- [ ] 1. Hetzner Object Storage のセットアップ (手動作業)
  - Hetzner Robot ダッシュボードで `aramakisai-backups` バケットを `fsn1` リージョンに作成する
  - S3 アクセスキーを発行し、`HETZNER_S3_ACCESS_KEY_ID` / `HETZNER_S3_SECRET_ACCESS_KEY` として Infisical に登録する
  - `scripts/push-secrets-to-infisical.sh` を使って登録し、クラスター側で ESO が取得できることを確認すれば完了
  - _Requirements: 1_

- [ ] 2. S3 認証情報 ExternalSecret の作成
  - `gitops/manifests/shared/eso/hetzner-s3-external-secret.yaml` を新規作成し、`hetzner-s3-credentials` Secret が `prod` namespace に展開されるよう定義する
  - `kubectl get secret hetzner-s3-credentials -n prod` で2キーが存在することを確認すれば完了
  - _Requirements: 1_

- [ ] 3. CloudNativePG バックアップ設定
- [ ] 3.1 (P) Authentik DB にバックアップ設定を追加
  - `gitops/manifests/prod/authentik/db-cluster.yaml` の `spec.backup.barmanObjectStore` に S3 設定 (destinationPath / endpointURL / s3Credentials / retentionPolicy: 7d) を追加する
  - `gitops/manifests/prod/authentik/scheduled-backup.yaml` を新規作成し、`schedule: "0 2 * * *"` で毎日 02:00 UTC にバックアップを取得するよう設定する
  - `kubectl get backup -n prod` で Authentik DB のバックアップが `completed` 状態になれば完了
  - _Requirements: 2_
  - _Boundary: authentik/db-cluster.yaml, authentik/scheduled-backup.yaml_

- [ ] 3.2 (P) Directus DB にバックアップ設定を追加
  - `gitops/manifests/prod/directus/db-cluster.yaml` に同様の S3 バックアップ設定を追加する
  - `gitops/manifests/prod/directus/scheduled-backup.yaml` を新規作成する
  - `kubectl get backup -n prod` で Directus DB のバックアップが `completed` 状態になれば完了
  - _Requirements: 2_
  - _Boundary: directus/db-cluster.yaml, directus/scheduled-backup.yaml_

- [ ] 4. VolSync による Stalwart PVC バックアップ
- [ ] 4.1 VolSync を ArgoCD Application として追加
  - `gitops/apps/prod/volsync.yaml` を新規作成し、VolSync Helm chart を `sync-wave: "-1"` でインストールする
  - `kubectl get pods -n volsync` で VolSync コントローラーが Running になれば完了
  - _Requirements: 3_

- [ ] 4.2 Stalwart restic ExternalSecret と ReplicationSource の作成
  - Infisical に `STALWART_RESTIC_REPOSITORY` / `STALWART_RESTIC_PASSWORD` を登録する
  - `gitops/manifests/prod/stalwart/restic-external-secret.yaml` を新規作成し、restic が必要とする Secret (`RESTIC_REPOSITORY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `RESTIC_PASSWORD`) を展開する
  - `gitops/manifests/prod/stalwart/replication-source.yaml` を新規作成し、`schedule: "0 3 * * *"` / `retain.daily: 7` でバックアップを設定する
  - `kubectl get replicationsource stalwart-backup -n prod -o jsonpath='{.status.lastSyncTime}'` に日時が表示されれば完了
  - _Requirements: 3_
  - _Depends: 4.1_

- [ ] 5. バックアップ動作の最終確認
  - CNPG: 翌日以降に `kubectl get backup -n prod` で両 DB のバックアップ履歴を確認する
  - VolSync: `kubectl describe replicationsource stalwart-backup -n prod` で `lastSyncDuration` と `lastSyncTime` を確認する
  - S3: Hetzner Robot ダッシュボードで `aramakisai-backups` バケット内にオブジェクトが作成されていることを確認する
  - 3 項目すべて確認できれば完了
  - _Requirements: 2, 3_
  - _Depends: 3.1, 3.2, 4.2_

- [ ] 6. Rclone Google Drive 同期のセットアップ
- [ ] 6.1 Google Drive Service Account の準備 (手動作業)
  - Google Cloud Console でプロジェクトを作成し Google Drive API を有効化する
  - Service Account を作成して JSON キーをダウンロードし、Google Drive の同期先フォルダ (`aramakisai-backups`) に「編集者」権限を付与する
  - SA JSON を Base64 エンコードしてから Infisical に登録する: `cat sa.json | base64 -w 0` の出力を `GOOGLE_SERVICE_ACCOUNT_JSON` として保存する (JSON をそのまま登録すると改行文字でパースエラーが起きるため Base64 を必ず使うこと)
  - `GDRIVE_FOLDER_ID` (フォルダ URL 末尾の ID) も Infisical に登録する
  - Infisical 上に2キーが確認できれば完了
  - _Requirements: 4_

- [ ] 6.2 rclone ExternalSecret・CronJob・ArgoCD Application を作成
  - `gitops/manifests/prod/rclone/external-secret.yaml` を新規作成し、`rclone-gdrive-secret` Secret (`SA_JSON` / `GDRIVE_FOLDER_ID`) が展開されるよう定義する
  - `gitops/manifests/prod/rclone/cronjob.yaml` を新規作成し、`schedule: "0 4 * * *"` / `concurrencyPolicy: Forbid` で S3 → Google Drive を rclone sync する CronJob を定義する
  - `gitops/apps/prod/rclone.yaml` に ArgoCD Application を定義する
  - `kubectl create job --from=cronjob/rclone-gdrive-sync rclone-test -n prod` で手動実行し、Job が `Completed` になれば完了
  - _Requirements: 4_
  - _Depends: 6.1_

- [ ] 6.3 Google Drive 同期の動作確認
  - Job 完了後、Google Drive の `aramakisai-backups` フォルダに `cnpg/` と `volsync/` のオブジェクトが存在することをブラウザで確認する
  - `kubectl logs job/rclone-test -n prod` でエラーなく転送完了ログが出力されていれば完了
  - _Requirements: 4_
  - _Depends: 6.2_

- [ ] 7. 復旧検証 (クラスター稼働中・部分障害を想定)
- [ ] 7.1 (P) CloudNativePG PITR リストア検証
  - staging 用の一時 Namespace (`restore-test`) を作成し、本番 DB には触れない環境でリストアを実施する
  - `authentik-db` の最新バックアップから `Cluster` リソースを `bootstrap.recovery` で再作成し、Pod が `Running` かつ `role=primary` になることを確認する
  - `kubectl exec` でリストア済み DB に接続し、テーブルとレコードが存在することを SQL で確認する
  - 確認後 `restore-test` Namespace を削除し、本番クラスターへの影響がないことを確認すれば完了
  - _Requirements: 2_
  - _Boundary: restore-test Namespace (本番非接触)_
  - _Depends: 5_

- [ ] 7.2 (P) VolSync restic リストア検証
  - Stalwart を一時停止 (`kubectl scale statefulset stalwart -n prod --replicas=0`) し、PVC 内のデータを退避する
  - `ReplicationDestination` リソースを手動トリガー (`trigger.manual`) で作成し、S3 から `stalwart-data` PVC へのリストアを実行する
  - `kubectl describe replicationdestination stalwart-restore -n prod` で `lastSyncTime` が更新され `latestImage` が設定されることを確認する
  - Stalwart を再起動 (`--replicas=1`) し、メールデータが復元されて SMTP/IMAP が正常に応答することを確認すれば完了
  - _Requirements: 3_
  - _Depends: 5_

- [ ] 8. フル DR 検証 (ノード全損シナリオ・目標 RTO 80〜100 分)
- [ ] 8.1 フェーズ 1: インフラ再構築 (~30 分)
  - `terraform taint` で全ノードを強制再作成対象にし、`terraform apply` で新規ノードを起動する
  - Tailscale tailnet にノードが登録されたことを確認してから `ansible-playbook k3s-bootstrap.yml` を実行する
  - Play 6 完了後、`kubectl get nodes` で 3 ノードが `Ready` かつ ArgoCD が `https://argocd.aramakisai.com` でアクセス可能になれば完了
  - _Requirements: 5_

- [ ] 8.2 フェーズ 2: ESO / ArgoCD 自動復旧の確認 (~10 分)
  - ArgoCD UI で全 Application が Synced になるまで待機する（ESO wave -1 → 全アプリ wave 0 の順）
  - `kubectl get secret -n prod` で `hetzner-s3-credentials`・`stalwart-secrets`・`authentik-secrets` 等が ESO によって再作成されていることを確認する
  - CNPG Cluster と Stalwart StatefulSet が起動しているが DB / メールデータが空であることを確認する（次フェーズへの前提確認）
  - _Requirements: 5_
  - _Depends: 8.1_

- [ ] 8.3 フェーズ 3: DB リストア (~20 分、並列実行可)
  - ArgoCD の Authentik / Directus Application を一時停止 (`argocd app pause`) し、空の CNPG Cluster を `kubectl delete cluster authentik-db directus-db -n prod` で削除する
  - 設計書の recovery 用 Cluster 定義を `kubectl apply` して S3 から PITR リストアを実行する
  - `kubectl get cluster -n prod` で両クラスターが `Cluster in healthy state` になったことを確認する
  - ArgoCD Application の pause を解除し、通常の `db-cluster.yaml` で上書き sync されることを確認すれば完了
  - _Requirements: 5_
  - _Depends: 8.2_

- [ ] 8.4 フェーズ 4: Stalwart メールデータ リストア (~10〜30 分)
  - `kubectl scale statefulset stalwart -n prod --replicas=0` で Stalwart を停止する
  - `ReplicationDestination` リソース (`trigger.manual`) を作成し、S3 restic リポジトリから `stalwart-data` PVC へリストアする
  - `kubectl describe replicationdestination` で `lastSyncTime` が記録されたことを確認してから Stalwart を再起動する
  - port 25 / 587 / 993 への SMTP・IMAP 接続が成功することを確認すれば完了
  - _Requirements: 5_
  - _Depends: 8.2_

- [ ] 8.5 フェーズ 5: サービス全体の疎通確認 (~10 分)
  - Roundcube (`https://webmail.aramakisai.com`) で Authentik ログインが成功することを確認する
  - テストメールを送受信し、Stalwart の IMAP フォルダにメールが届くことを確認する
  - Directus (`https://api.aramakisai.com`) の管理画面にログインし、既存データが表示されることを確認する
  - 上記 3 項目すべて確認できれば DR 完了
  - _Requirements: 5_
  - _Depends: 8.3, 8.4_
