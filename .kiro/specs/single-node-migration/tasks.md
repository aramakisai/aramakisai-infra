# タスク定義 (Tasks) - シングルノード移行

## 実施結果サマリー（2026-06-03 移行完了）

| サービス | 状態 | 備考 |
|---------|------|------|
| Authentik DB | ✅ B2 recovery 成功 | `skipEmptyWalArchiveCheck: enabled` + `imageName: postgresql:16.8` が必要だった |
| Directus DB | ⚠️ initdb（空DB）| WAL アーカイブが旧クラスターで未設定。DR 時も空DB起動 |
| Stalwart | ✅ VolSync リストア成功 | `replication-destination.yaml` を適用して復元 |
| Roundcube | ✅ 起動（セッション空） | 設計通り |

**重要な設計変更**（次回以降の参考）:
- CNPG: `imageName: ghcr.io/cloudnative-pg/postgresql:16.8` を必ず明示すること
- CNPG: `cnpg.io/skipEmptyWalArchiveCheck: enabled`（`"true"` では効かない）
- Infisical: `.infisical.json` の `defaultEnvironment: "prod"` を確認すること
- kubectl: `make kubectl ARGS="..."` を使う（KUBECONFIG を Infisical から /tmp に展開）
- DR ランブック: `docs/dr-runbook.md` を参照
- DR 自動化: `.github/scripts/recovery.sh` + `.github/workflows/dr-recovery.yml`（GitHub Actions 移行済み）

## タスク一覧

- [x] 1. 現クラスターからのデータバックアップ
- [x] 1.1 (P) Authentik PostgreSQL — B2 recovery bootstrap で対応（手順不要）
  - `db-cluster.yaml` の `bootstrap.recovery` + `externalClusters` で新クラスター初回起動時に B2 の最新バックアップから自動リストアされる
  - 手動の pg_dump・psql は不要（DR と同じ経路）
  - 確認: `kubectl get cluster authentik-db -n prod -o jsonpath='{.status.phase}'` が `Cluster in healthy state` になれば完了
  - _Requirements: 2.1_

- [x] 1.2 (P) Directus PostgreSQL — B2 recovery bootstrap で対応（手順不要）
  - `db-cluster.yaml` の `bootstrap.recovery` + `externalClusters` で新クラスター初回起動時に B2 の最新バックアップから自動リストアされる
  - 手動の pg_dump・psql は不要（DR と同じ経路）
  - 確認: `kubectl get cluster directus-db -n prod -o jsonpath='{.status.phase}'` が `Cluster in healthy state` になれば完了
  - _Requirements: 2.2_

- [x] 1.3 Roundcube SQLite — 対応不要
  - SQLite にはセッションデータのみ保存されており、移行後にユーザーが再ログインすれば問題ない
  - バックアップ・リストアともにスキップする
  - _Requirements: 2.3_

- [x] 2. GitOps・IaC のシングルノード対応変更をコミット
- [x] 2.1 (P) CNPG DB クラスターをシングルインスタンス化する
  - `gitops/manifests/prod/authentik/db-cluster.yaml` の `instances` を 1 に変更し、`affinity` ブロックを削除する（`backup` 設定は backup スペックで実装済みのため変更不要）
  - `gitops/manifests/prod/directus/db-cluster.yaml` に同様の変更を適用する
  - `kubectl apply --dry-run=client -f db-cluster.yaml` がエラーなく通れば完了
  - _Requirements: 3.1, 3.4_
  - _Boundary: gitops/manifests/prod/authentik, gitops/manifests/prod/directus_

- [x] 2.2 (P) Stalwart の nodeSelector を削除する
  - `gitops/manifests/prod/stalwart/statefulset.yaml` の `nodeSelector` ブロック全体を削除する（シングルノードなので不要。HA 復帰時もノード名依存をなくすため削除）
  - S3 認証情報 ExternalSecret（`b2-credentials`）は backup スペックで `gitops/manifests/shared/eso/b2-external-secret.yaml` として実装済みのため作成不要
  - `kubectl apply --dry-run=client -f statefulset.yaml` がエラーなく通れば完了
  - _Requirements: 3.2, 3.3_
  - _Boundary: gitops/manifests/prod/stalwart_

- [x] 2.3 (P) Terraform ノード定義をシングル CX33 に変更する
  - `terraform/main.tf` の `local.nodes` を `prod-node-1` 単体に変更し `server_type` を `cx33` に変更する
  - `labels` の `role` 分岐（cp-node 判定）を削除して固定値 `"server"` にする
  - `terraform/outputs.tf` から `cp_node_ipv6` と `prod_node_2_ipv6` を削除する
  - `terraform plan -var-file="secrets.tfvars"` で「3 to destroy, 1 to add」となれば完了（cp-node・旧 prod-node-1・prod-node-2 の 3 台が削除対象、新 CX33 prod-node-1 が追加対象）
  - _Requirements: 1.1, 1.2_
  - _Boundary: terraform/main.tf, terraform/outputs.tf_

- [x] 2.4 Ansible インベントリをシングルノード構成に変更しコミットする
  - `ansible/inventory/tailscale.yml` の `k3s_server_worker` グループ全体を削除する
  - `k3s_server` グループの `prod-node-1` のみ残し、`k3s_cluster_init: true`・`k3s_private_ip: 10.0.1.1` を設定する
  - `ansible -i inventory/tailscale.yml k3s_server -m ping --check` が接続先1台を示せば完了（新ノード稼働後に確認）
  - 2.1〜2.3, 2.5 の変更とまとめて `git commit && git push` し、ArgoCD が新設定を参照できる状態にする
  - _Requirements: 1.2
  - _Boundary: ansible/inventory/tailscale.yml_
  - _Depends: 2.1, 2.2, 2.3_

- [x] 2.5 不要な Ansible ロールを削除する
  - `ansible/roles/k3s-agent/` ディレクトリを削除し、デッドコードをクリーンアップする
  - `git status` で `k3s-agent` の削除が確認されれば完了
  - _Boundary: ansible/roles/k3s-agent_

- [x] 3. 新クラスターのプロビジョニング
- [x] 3.1 Tailscale デバイスを削除してから terraform apply で新ノードを作成する
  - `.env` を source して全必須環境変数（`HCLOUD_TOKEN`・`TF_VAR_k3s_token`・`TAILSCALE_API_KEY`・`TAILSCALE_TAILNET` 等）が設定されていることを確認する
  - **terraform apply より先に** Tailscale API で cp-node・prod-node-1・prod-node-2 を削除する（`ephemeral=false` のため Hetzner ノード削除後も tailnet に残存し、新ノードが `prod-node-1-1` として登録されるのを防ぐ）
    ```bash
    for HOST in cp-node prod-node-1 prod-node-2; do
      ID=$(curl -s -H "Authorization: Bearer $TAILSCALE_API_KEY" \
        "https://api.tailscale.com/api/v2/tailnet/$TAILSCALE_TAILNET/devices" \
        | jq -r --arg h "$HOST" '.devices[] | select(.hostname == $h) | .id')
      [ -n "$ID" ] && curl -s -X DELETE -H "Authorization: Bearer $TAILSCALE_API_KEY" \
        "https://api.tailscale.com/api/v2/device/$ID"
    done
    ```
  - `terraform apply -var-file="secrets.tfvars"` を実行する
  - Hetzner Cloud コンソールで CX33 の `prod-node-1` が作成され、旧3ノードが削除されていることを確認する
  - Tailscale 管理コンソールで `prod-node-1`（サフィックスなし）として登録されていることを確認する
  - `dig mail.aramakisai.com AAAA` が新ノードの IPv6 を返せば完了
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 3.2 Ansible でシングルノード K3s をブートストラップする
  - `.env` を source して `INFISICAL_CLIENT_ID`・`INFISICAL_CLIENT_SECRET`・`ARGOCD_GITHUB_DEPLOY_KEY`・`K3S_TOKEN`・`CLOUDFLARE_TUNNEL_TOKEN`・`CLOUDFLARE_TUNNEL_ID` が設定されていることを確認する
  - `ansible -i ansible/inventory/tailscale.yml k3s_server -m ping` で prod-node-1 への疎通を確認する
  - `ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml` を実行する（Play 2 は自動スキップ）
  - `kubectl get nodes` で prod-node-1 が `Ready` 状態であれば完了
  - _Requirements: 1.2_
  - _Depends: 3.1_

- [x] 4. ArgoCD sync・シークレット注入の確認とデータリストア
- [x] 4.1 ArgoCD sync とシークレット注入が正常であることを確認する
  - `kubectl get secret infisical-auth -n argocd -o jsonpath='{.data.clientId}' | base64 -d` が空でないことを確認する
  - `kubectl get secret aramakisai-infra-repo -n argocd -o jsonpath='{.data.sshPrivateKey}' | base64 -d | wc -c` が 0 より大きいことを確認する（2026-06-02 インシデント教訓: 両 Secret が空だと ESO 全停止・ArgoCD git 接続不可になる）
  - いずれかが空の場合は `.env` を source した上で手動 patch し、全 ExternalSecret を `force-sync` アノテーションで強制再同期する
  - `kubectl get applications -n argocd` で全 Application が `Synced / Healthy` になれば完了
  - _Requirements: 3.1, 3.3, 3.4_
  - _Depends: 3.2_

- [x] 4.2 (P) Authentik DB リストア確認とサービス再起動
  - CNPG が B2 から自動リストア済みのため psql 実行は不要
  - `kubectl get cluster authentik-db -n prod -o jsonpath='{.status.phase}'` が `Cluster in healthy state` であることを確認する
  - リストアに失敗している場合は `kubectl describe cluster authentik-db -n prod` でログを確認し、`b2-credentials` Secret が存在するか確認する
  - `kubectl rollout restart deployment/authentik-server deployment/authentik-worker -n prod` を実行する
  - `https://idp.aramakisai.com` にブラウザアクセスしてログインが成功すれば完了
  - _Requirements: 2.1_
  - _Boundary: Authentik DB, Authentik Server_
  - _Depends: 4.1_

- [x] 4.3 (P) Directus DB リストア確認とサービス再起動
  - CNPG が B2 から自動リストア済みのため psql 実行は不要
  - `kubectl get cluster directus-db -n prod -o jsonpath='{.status.phase}'` が `Cluster in healthy state` であることを確認する
  - リストアに失敗している場合は `kubectl describe cluster directus-db -n prod` でログを確認する
  - `kubectl rollout restart deployment/directus -n prod` を実行する
  - `https://api.aramakisai.com/admin` にアクセスしてコンテンツが表示されれば完了
  - _Requirements: 2.2_
  - _Boundary: Directus DB, Directus Deployment_
  - _Depends: 4.1_

- [x] 4.4 Roundcube — 対応不要
  - セッションデータのみのため移行後は空の状態で起動し、ユーザーが再ログインする
  - `https://webmail.aramakisai.com` にアクセスして画面が表示されれば完了（データリストアなし）
  - _Requirements: 2.3_
  - _Depends: 4.1_

- [x] 4.5 (P) Stalwart メールデータを VolSync からリストアする
  - Stalwart StatefulSet が PVC（`stalwart-data`）を作成済みであることを確認する（`kubectl get pvc stalwart-data -n prod`）
  - PVC がマウント中だとリストアできないため、先に Stalwart を停止する: `kubectl scale statefulset stalwart -n prod --replicas=0`
  - `scripts/volsync-restore.sh` またはリストア用 `ReplicationDestination` を適用し、S3 restic から `stalwart-data` PVC へデータをリストアする
  - `kubectl wait replicationdestination/stalwart-restore -n prod --for=condition=Reconciled --timeout=30m` でリストア完了を待つ
  - リストア完了後に Stalwart を再起動: `kubectl scale statefulset stalwart -n prod --replicas=1`
  - SMTP(587)/IMAP(993) ポートで疎通が確認されれば完了
  - _Requirements: 2.4_
  - _Boundary: Stalwart StatefulSet, Stalwart PVC_
  - _Depends: 4.1_

- [x] 5. Raspberry Pi 復旧サービスの実装 → **GitHub Actions に移行済み（2026-06-04）**

> Raspberry Pi を廃止し GitHub Actions (`repository_dispatch: dr-recovery`) に移行した。
> `.github/workflows/dr-recovery.yml` + `.github/scripts/recovery.sh` が実体。
> `raspberry-pi/` ディレクトリは削除済み。
- [x] 5.1 (P) Recovery Webhook Service を実装する（recover.py）
  - `raspberry-pi/recovery/recover.py` を新規作成し、`POST /recover` エンドポイントを Flask で実装する
  - `/tmp/recovery.lock` によるロック機構を実装し、多重実行時に HTTP 409 を返すようにする
  - Grafana Cloud Webhook の `status: "firing"` のみを復旧トリガーとし、`status: "resolved"` は無視する
  - `curl -X POST http://localhost:8080/recover -d '{"status":"firing","alerts":[]}' -H 'Content-Type: application/json'` が `{"status":"recovery_started"}` を返せば完了
  - _Requirements: 4.2, 4.3, 4.4_
  - _Boundary: raspberry-pi/recovery/recover.py_

- [x] 5.2 (P) Recovery Script を実装する（recovery.sh）
  - `raspberry-pi/recovery/recovery.sh` を新規作成する
  - スクリプト冒頭で必須環境変数（`INFISICAL_CLIENT_ID`・`K3S_TOKEN`・`ARGOCD_GITHUB_DEPLOY_KEY`・`TAILSCALE_API_KEY`・`TAILSCALE_TAILNET` 等）の存在チェックを行い、未設定の場合はエラー終了する
  - **terraform apply より前に** Tailscale API で `prod-node-1` デバイスを削除する（`ephemeral=false` により障害ノードが tailnet に残存し、新ノードが `prod-node-1-1` として登録されるのを防ぐ）
  - Terraform Cloud API（`POST /api/v2/runs`）でプランを作成・適用し、完了を待機する
  - Tailscale API で `prod-node-1`（サフィックスなし）の登録を最大10分ポーリングし、タイムアウト時はエラー終了してロックを解放する
  - `ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml` を実行する
  - スクリプトが正常終了したとき `/tmp/recovery.lock` が削除されていれば完了
  - _Requirements: 4.3, 4.4, 4.5_
  - _Boundary: raspberry-pi/recovery/recovery.sh_

- [x] 5.3 systemd unit を設定して Recovery Service を常駐させる
  - `raspberry-pi/recovery/recovery.service` を新規作成し、`infisical run --` でサービスを起動する設定にする
  - `/opt/recovery/.infisical-auth` に Infisical 認証情報のみ記載し `chmod 600` で保護する（シークレット本体は Infisical から自動注入されるため置かない）
    ```
    INFISICAL_CLIENT_ID=...
    INFISICAL_CLIENT_SECRET=...
    INFISICAL_PROJECT_ID=...
    ```
  - `systemctl enable --now recovery` で自動起動を有効化する
  - `systemctl status recovery` が `active (running)` を示せば完了
  - _Requirements: 4.2, 4.3_
  - _Boundary: raspberry-pi/recovery/recovery.service_
  - _Depends: 5.1, 5.2_

- [ ] 6. 移行後検証と Grafana Cloud Webhook 連携
- [ ] 6.1 移行後の全サービス動作確認と CNPG バックアップ確認を行う
  - `kubectl top nodes` で prod-node-1 のメモリ使用率が 60% 以下であることを確認する
  - `kubectl get applications -n argocd` で全 Application が `Synced / Healthy` であることを確認する
  - Authentik（`https://idp.aramakisai.com`）・Directus（`https://api.aramakisai.com/admin`）・Roundcube（`https://webmail.aramakisai.com`）への疎通を確認する
  - `dig mail.aramakisai.com AAAA` が新ノードの IPv6 を返すことを確認する
  - `kubectl get scheduledbackup -n prod` で ScheduledBackup リソースが存在することを確認する（`backup`スペック完了前提）
  - 上記すべてが確認できれば完了
  - _Requirements: 5.1, 5.2_

- [x] 6.2 Grafana Cloud Webhook 連携の設定 → **GitHub Actions 移行に伴い再定義済み（2026-06-04）**

  - Grafana Cloud Contact Point の Webhook 先を GitHub API (`repository_dispatch`) に変更する設計に移行
  - 詳細設定・アラート条件（2シグナル AND）は `dr-automation` スペック REQ-01 に引き継ぎ
  - _Depends: dr-automation スペックの実装_
