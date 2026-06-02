# タスク定義 (Tasks) - シングルノード移行

## タスク一覧

- [ ] 1. 現クラスターからのデータバックアップ
- [ ] 1.1 (P) Authentik PostgreSQL をダンプする
  - 現クラスターの `authentik-db-rw` Service に port-forward し `pg_dump` を実行する
  - 出力先: `/tmp/authentik-backup.sql`
  - `wc -l /tmp/authentik-backup.sql` が 0 より大きければ完了
  - _Requirements: 2.1_
  - _Boundary: 現クラスター / Authentik DB_

- [ ] 1.2 (P) Directus PostgreSQL をダンプする
  - 現クラスターの `directus-db-rw` Service に port-forward し `pg_dump` を実行する
  - 出力先: `/tmp/directus-backup.sql`
  - `wc -l /tmp/directus-backup.sql` が 0 より大きければ完了
  - _Requirements: 2.2_
  - _Boundary: 現クラスター / Directus DB_

- [ ] 1.3 (P) Roundcube SQLite をコピーする
  - `kubectl cp` で Roundcube Pod の `/var/roundcube/db/` を `/tmp/roundcube-db/` にコピーする
  - `/tmp/roundcube-db/` 配下にファイルが存在すれば完了
  - _Requirements: 2.3_
  - _Boundary: 現クラスター / Roundcube Pod_

- [ ] 2. GitOps・IaC のシングルノード対応変更をコミット
- [ ] 2.1 (P) CNPG DB クラスターをシングルインスタンス化し S3 バックアップ設定を追加する
  - `gitops/manifests/prod/authentik/db-cluster.yaml` の `instances` を 1 に変更し、`affinity` ブロックを削除し、`backup.barmanObjectStore` を追加する
  - `gitops/manifests/prod/directus/db-cluster.yaml` に同様の変更を適用する（`destinationPath` は `cnpg/directus`）
  - `kubectl apply --dry-run=client -f db-cluster.yaml` がエラーなく通れば完了
  - _Requirements: 3.1, 3.4_
  - _Boundary: gitops/manifests/prod/authentik, gitops/manifests/prod/directus_

- [ ] 2.2 (P) Stalwart の nodeSelector を削除し S3 認証情報 ExternalSecret を作成する
  - `gitops/manifests/prod/stalwart/statefulset.yaml` の `nodeSelector` ブロック全体を削除する
  - `gitops/manifests/prod/hetzner-s3-credentials.yaml` を新規作成し、`HETZNER_S3_ACCESS_KEY` と `HETZNER_S3_SECRET_KEY` を Infisical から取得する ExternalSecret を定義する
  - `kubectl apply --dry-run=client -f hetzner-s3-credentials.yaml` がエラーなく通れば完了
  - _Requirements: 3.2, 3.3_
  - _Boundary: gitops/manifests/prod/stalwart, gitops/manifests/prod/_

- [ ] 2.3 (P) Terraform ノード定義をシングル CX33 に変更する
  - `terraform/main.tf` の `local.nodes` を `prod-node-1` 単体に変更し `server_type` を `cx33` に変更する
  - `labels` の `role` 分岐（cp-node 判定）を削除して固定値 `"server"` にする
  - `terraform/outputs.tf` から `cp_node_ipv6` と `prod_node_2_ipv6` を削除する
  - `terraform plan -var-file="secrets.tfvars"` で「2 to destroy, 1 to add」となれば完了
  - _Requirements: 1.1, 1.2_
  - _Boundary: terraform/main.tf, terraform/outputs.tf_

- [ ] 2.4 Ansible インベントリをシングルノード構成に変更しコミットする
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

- [ ] 3. 新クラスターのプロビジョニング
- [ ] 3.1 terraform apply で新ノードを作成し旧3ノードを削除する
  - `.env` を source して全必須環境変数（`HCLOUD_TOKEN`・`TF_VAR_k3s_token` 等）が設定されていることを確認する
  - `terraform apply -var-file="secrets.tfvars"` を実行する
  - Hetzner Cloud コンソールで CX33 の `prod-node-1` が作成され、旧3ノードが削除されていることを確認する
  - Tailscale 管理コンソールで `prod-node-1` が登録されたことを確認し、旧ノード（cp-node・prod-node-2）を手動削除する
  - `dig mail.aramakisai.com AAAA` が新ノードの IPv6 を返せば完了
  - _Requirements: 1.1, 1.2, 1.3_

- [ ] 3.2 Ansible でシングルノード K3s をブートストラップする
  - `.env` を source して `INFISICAL_CLIENT_ID`・`INFISICAL_CLIENT_SECRET`・`ARGOCD_GITHUB_DEPLOY_KEY`・`K3S_TOKEN`・`CLOUDFLARE_TUNNEL_TOKEN`・`CLOUDFLARE_TUNNEL_ID` が設定されていることを確認する
  - `ansible -i ansible/inventory/tailscale.yml k3s_server -m ping` で prod-node-1 への疎通を確認する
  - `ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml` を実行する（Play 2 は自動スキップ）
  - `kubectl get nodes` で prod-node-1 が `Ready` 状態であれば完了
  - _Requirements: 1.2_
  - _Depends: 3.1_

- [ ] 4. ArgoCD sync・シークレット注入の確認とデータリストア
- [ ] 4.1 ArgoCD sync とシークレット注入が正常であることを確認する
  - `kubectl get secret infisical-auth -n argocd -o jsonpath='{.data.clientId}' | base64 -d` が空でないことを確認する
  - 空の場合は `.env` を source した上で `kubectl -n argocd patch secret infisical-auth` で再注入し、全 ExternalSecret を `force-sync` アノテーションで強制再同期する
  - `kubectl get applications -n argocd` で全 Application が `Synced / Healthy` になれば完了
  - _Requirements: 3.1, 3.3, 3.4_
  - _Depends: 3.2_

- [ ] 4.2 (P) Authentik DB にデータをリストアしサービスを再起動する
  - CNPG の `authentik-db-rw` Service に port-forward し `psql < /tmp/authentik-backup.sql` を実行する
  - `kubectl rollout restart deployment/authentik-server deployment/authentik-worker -n prod` を実行する
  - `https://idp.aramakisai.com` にブラウザアクセスしてログインが成功すれば完了
  - _Requirements: 2.1_
  - _Boundary: Authentik DB, Authentik Server_
  - _Depends: 4.1_

- [ ] 4.3 (P) Directus DB にデータをリストアしサービスを再起動する
  - CNPG の `directus-db-rw` Service に port-forward し `psql < /tmp/directus-backup.sql` を実行する
  - `kubectl rollout restart deployment/directus -n prod` を実行する
  - `https://api.aramakisai.com/admin` にアクセスしてコンテンツが表示されれば完了
  - _Requirements: 2.2_
  - _Boundary: Directus DB, Directus Deployment_
  - _Depends: 4.1_

- [ ] 4.4 (P) Roundcube SQLite をリストアしサービスを再起動する
  - `kubectl cp /tmp/roundcube-db/. prod/<roundcube-pod>:/var/roundcube/db/` を実行する
  - `kubectl rollout restart deployment/roundcube -n prod` を実行する
  - `https://webmail.aramakisai.com` にアクセスして画面が表示されれば完了
  - _Requirements: 2.3_
  - _Boundary: Roundcube Deployment_
  - _Depends: 4.1_

- [ ] 4.5 (P) Stalwart メールデータを VolSync からリストアする
  - `./scripts/test-volsync-restore.sh` 相当の手順で `ReplicationDestination` を作成し、S3 restic から `stalwart-data` PVC へデータをリストアする
  - `kubectl rollout restart statefulset/stalwart -n prod` を実行する
  - メールサーバーが起動し、SMTP(587)/IMAP(993) ポートで疎通が確認されれば完了
  - _Requirements: 2.4_
  - _Boundary: Stalwart StatefulSet, Stalwart PVC_
  - _Depends: 4.1_

- [ ] 5. Raspberry Pi 復旧サービスの実装
- [ ] 5.1 (P) Recovery Webhook Service を実装する（recover.py）
  - `raspberry-pi/recovery/recover.py` を新規作成し、`POST /recover` エンドポイントを Flask で実装する
  - `/tmp/recovery.lock` によるロック機構を実装し、多重実行時に HTTP 409 を返すようにする
  - Grafana Cloud Webhook の `status: "firing"` のみを復旧トリガーとし、`status: "resolved"` は無視する
  - `curl -X POST http://localhost:8080/recover -d '{"status":"firing","alerts":[]}' -H 'Content-Type: application/json'` が `{"status":"recovery_started"}` を返せば完了
  - _Requirements: 4.2, 4.3, 4.4_
  - _Boundary: raspberry-pi/recovery/recover.py_

- [ ] 5.2 (P) Recovery Script を実装する（recovery.sh）
  - `raspberry-pi/recovery/recovery.sh` を新規作成する
  - スクリプト冒頭で必須環境変数（`INFISICAL_CLIENT_ID`・`K3S_TOKEN`・`ARGOCD_GITHUB_DEPLOY_KEY` 等）の存在チェックを行い、未設定の場合はエラー終了する
  - Terraform Cloud API（`POST /api/v2/runs`）でプランを作成・適用し、完了を待機する
  - Tailscale API で `prod-node-1` の登録を最大10分ポーリングし、タイムアウト時はエラー終了してロックを解放する
  - `ansible-playbook -i ansible/inventory/tailscale.yml ansible/playbooks/k3s-bootstrap.yml` を実行する
  - スクリプトが正常終了したとき `/tmp/recovery.lock` が削除されていれば完了
  - _Requirements: 4.3, 4.4, 4.5_
  - _Boundary: raspberry-pi/recovery/recovery.sh_

- [ ] 5.3 systemd unit を設定して Recovery Service を常駐させる
  - `raspberry-pi/recovery/recovery.service` を新規作成し、`/opt/recovery/.env` を `EnvironmentFile` として指定する
  - `/opt/recovery/.env` に必要な全環境変数を記載し `chmod 600` で保護する
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

- [ ] 6.2 Raspberry Pi と Grafana Cloud の Webhook 連携を設定・確認する
  - Grafana Cloud の Alerting → Contact Points で「Webhook」タイプを追加し URL を `http://<pi-tailscale-ip>:8080/recover` に設定する
  - `idp.aramakisai.com` の Synthetic Monitoring チェックが存在することを確認する（`monitoring`スペック前提）
  - Pi 上で `curl -X POST http://localhost:8080/recover -d '{"status":"firing","alerts":[{"labels":{"alertname":"ServiceDown"}}]}' -H 'Content-Type: application/json'` を実行し `{"status":"recovery_started"}` が返ることを確認する
  - `recovery.sh` が実行されて `/tmp/recovery.lock` が作成されることを確認し、スクリプトが完了したらロックが解放されることを確認する
  - Pi の Tailscale IP から `prod-node-1` への SSH 接続が通ることを確認する
  - 上記すべてが確認できれば完了
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 5.3_
  - _Depends: 5.3, 6.1_
