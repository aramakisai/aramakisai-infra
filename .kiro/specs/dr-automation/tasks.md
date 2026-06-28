# Implementation Plan

> **2026-06-27 スコープ改訂**: 以下の判断に基づきタスクを整理した。
> - Grafana Cloud はアカウント削除済み。DR トリガーは `dr-trigger.yml`（GitHub Actions完結型）に引き継ぎ済み。
> - Falco は過検知が多く、自動 DR dispatch のトリガーとして使うのはリスクが高い。
> - ランサムウェア等の侵害時は Infisical の全シークレットローテーションが必要なため、`intrusion-response.yml` から `recovery.sh` を自動呼び出しするのは危険（ローテーション前に再構築すると新ノードも即座に危険）。
> - `intrusion-response.yml` のスコープは「証拠保全・ネットワーク遮断・ローテーション手順通知」に限定し、再構築は人間が手動で実施する。

- [x] 1. recovery.sh の Stalwart→mailserver 参照を修正し、独立リリース可能な単位として完成させる
  - `.github/scripts/recovery.sh` のヘッダコメント（L10）・Step 7 内のコメント（L227, L264）・ログ出力（L230）・StatefulSet/PVC/Secret/ReplicationDestination 参照をすべて `stalwart*` から `mailserver`／`mailserver-data`／`mailserver-restic-secret`／`mailserver-restore` に置き換える
  - コメント内の「B2 からリストア」表記を実際のバックアップ先（Hetzner Object Storage）に整合させる
  - 本タスクは REQ-03 以降の自己修復ステップ追加（タスク 4）の着手を待たず単独でコミット・マージ・リリースできる変更単位とする
  - `grep -i stalwart .github/scripts/recovery.sh` が一致なしになることを確認できれば完了
  - _Requirements: 00-1, 00-2_

- [x] 2. DR ワークフローが動作するための運用基盤をセットアップする
- [x] 2.1 (P) GitHub Actions Secrets と Tailscale tag:ci を設定する
  - リポジトリの Settings → Secrets and variables → Actions に `INFISICAL_CLIENT_ID`／`INFISICAL_CLIENT_SECRET`／`INFISICAL_PROJECT_ID`／`TS_OAUTH_CLIENT_ID`／`TS_OAUTH_SECRET` を登録する（`TAILSCALE_API_KEY`・`TAILSCALE_TAILNET`・`DISCORD_OPS_WEBHOOK_URL` は observability-v2 で登録済み）
  - Tailscale ACL の `tagOwners` に `tag:ci` を追加し、Machines: Create 権限のみを持つ専用 OAuth Client を新規発行する
  - `dr-recovery.yml` を手動 `repository_dispatch` で発火し、Tailscale 接続ステップと Infisical シークレット読み込みステップが成功することを確認できれば完了
  - _Requirements: 02-1, 02-2_
  - _Boundary: OperationalSetupSecrets_

- [x] 3. mail-tls Certificate を GitOps 管理下に追加する
- [x] 3.1 (P) mail-tls 発行用 Certificate リソースを追加する
  - `gitops/manifests/prod/mailserver/certificate.yaml` を新規作成し、既存の `letsencrypt-prod` ClusterIssuer（Cloudflare DNS-01）を再利用して `mail.aramakisai.com` 単一ドメインの証明書を発行する定義にする
  - 既存の `mailserver` ArgoCD Application（`gitops/apps/prod/mailserver.yaml`）配下に含め、新規 Application は作らない
  - main へ push し ArgoCD sync 後、`mail-tls` Secret が `prod` namespace に生成され mailserver Pod が `Ready` になることを確認できれば完了
  - _Requirements: 03-2_
  - _Boundary: MailTlsCertificate_

  > **注記**: Falco・falcosidekick は `gitops/apps/prod/falco.yaml` および `gitops/helm-values/prod/falco.yaml` でデプロイ済み（旧 Task 3.2 に相当）。Discord 通知は稼働中。intrusion-response 自動 dispatch は過検知リスクと falcosidekick のペイロード非互換性のため実装しない。

- [x] 4. recovery.sh に既知の手動介入点を自己修復するステップを追加する
- [x] 4.1 CNPG の古い Job をベストエフォートでクリーンアップするステップを追加する（Step0）
  - Ansible 実行前に `cnpg.io/cluster=authentik-db`／`cnpg.io/cluster=directus-db` ラベル付きの古い Job を削除する処理を追加する
  - 対象クラスターが存在しない初回実行時は失敗してもスクリプト全体を停止させない non-fatal 処理にする
  - 既存クラスターに対して当該ステップのみ手動実行し、古い Job が削除されること、クラスター不在時でも後続ステップに進むことを確認できれば完了
  - _Requirements: 03-3_
  - _Depends: 1_

- [x] 4.2 infisical-auth／Deploy Key の空チェックと自己修復ステップを追加する（Step5a）
  - 既存 Step5（ArgoCD/CNPG healthy 待ち）の直後に、`infisical-auth` の `clientId` と `aramakisai-infra-repo` の `sshPrivateKey` が空でないことを確認する処理を追加する
  - いずれかが空の場合は Infisical から再取得して `kubectl apply` し、ESO の ExternalSecret に `force-sync` annotation を付与して再同期させる
  - 値を意図的に空にした状態で当該ステップを実行すると自動的に再作成され ESO が force-sync されることを確認できれば完了
  - _Requirements: 03-1_

- [x] 4.3 mail-tls 証明書の自己修復ステップを追加する（Step5b）
  - mailserver Pod が `ContainerCreating` のまま 2 分以上停止している場合に `mail-tls` Secret の存在を確認する処理を追加する
  - 存在しない場合は `gitops/manifests/prod/mailserver/certificate.yaml`／`external-secret.yaml`／`restic-external-secret.yaml` を `kubectl apply` する
  - `mail-tls` Secret を意図的に削除した状態で当該ステップを実行すると自動的に再作成され mailserver Pod が `Ready` になることを確認できれば完了
  - _Requirements: 03-2_
  - _Depends: 3.1_

- [x] 4.4 Directus DB リストア確認ログを末尾に追加する（Step7末尾）
  - Step7 完了時に `directus-db` Cluster の `status.firstRecoverabilityPoint` と簡易な行数確認クエリの結果をログ出力する処理を追加する
  - 既存クラスターに対して当該ステップを手動実行し、WAL リストア完了を示すログが標準エラー出力に出力されることを確認できれば完了
  - _Requirements: 03-4_

- [x] 5. intrusion-response ワークフローを新規作成する
- [x] 5.1 forensics・isolate・ローテーション通知ジョブを実装する
  - `.github/workflows/intrusion-response.yml` を新規作成し、`on.workflow_dispatch` のみをトリガーにする（自動 dispatch は行わない）
  - inputs として `namespace`（必須）と `pod_selector`（任意、デフォルト `""`）を定義する
  - **forensics ジョブ**: `kubectl logs`・`kubectl get events`・`kubectl get networkpolicy` で対象 namespace のログと状態を取得し、`actions/upload-artifact` で 90 日保持の Artifacts に保存する
  - **isolate ジョブ**: 指定 namespace に対して egress/ingress を全拒否する `NetworkPolicy` を `kubectl apply` する（forensics 完了後に実行）
  - **notify ジョブ**: isolate 完了後、Discord Webhook（`DISCORD_OPS_WEBHOOK_URL`）へ以下を通知する
    - 侵害を検知した namespace・pod_selector
    - ローテーション必須シークレット一覧（`steering/tech.md` 記載の全 Infisical キーを列挙）
    - 「再構築前にすべてのシークレットをローテーションしてから `dr-recovery` を手動 dispatch すること」の注意書き
  - `workflow_dispatch` で手動発火し、Artifacts が生成され NetworkPolicy が適用され Discord 通知が届くことを確認できれば完了
  - _Requirements: 04-2_

- [x] 6. dr-runbook と steering ドキュメントを自動化済みの内容に更新する
  - `docs/dr-runbook.md` の「既知の想定内事象」のうち本スペックで自動化した項目（CNPG Job クリーンアップ・infisical-auth/Deploy Key 自己修復・mail-tls 自己修復・Directus 確認ログ）を「自動修復済み」に書き換える
  - `docs/dr-runbook.md` に `intrusion-response.yml` の運用手順（手動 dispatch 手順・ローテーション後に `dr-recovery` を手動発火する手順）を追記する
  - `.kiro/steering/dr.md` に Falco（Discord 通知）および `intrusion-response.yml`（手動発火・証拠保全・ネットワーク遮断）への参照を追加する
  - 更新後のドキュメントを読み、自動化済み項目と手動フォールバック項目が現状の実装と一致していることを確認できれば完了
  - _Requirements: 00-1, 03-1, 03-2, 03-3, 03-4, 04-2_
  - _Depends: 1, 4, 5_

- [x] 7. DR・侵入対応フローのエンドツーエンド検証を行う
- [x] 7.1 recovery.sh 全体の通し実行で RTO 30 分以内と Step7 の成功を確認する
  - **ローカル統合テスト完了（2026-06-27）**: `.github/scripts/dr-local-test.sh` で k3d クラスターに対して自己修復ステップを検証（PASS=13 FAIL=0）
    - ✓ Step0 / Step6a / Step6b / Step7末尾 はローカル検証済み
  - **KVM テスト試行 (2026-06-27) → 断念**: libvirt nftables NAT 問題で断念。ログは tasks.md 旧版参照。
  - **k3d フルスタック検証完了（2026-06-27〜28）**: `.github/scripts/dr-k3d-fullstack.sh` で k3d + HOS 実データを使って全スタックを検証
    - ✓ ArgoCD v3.4.4 + ESO ClusterSecretStore 起動・sync 確認
    - ✓ CNPG WAL リカバリ: authentik-db（215テーブル）、vaultwarden-db（29テーブル）、presence-db（2テーブル）、directus-db（本番も空、WAL restore自体は成功）
    - ✓ VolSync mailserver restic リストア成功（229ファイル）
    - **発見した問題と修正**:
      1. `bootstrap.initdb` → `bootstrap.recovery` バグ修正（全4 DB）: コミット 532e18c
      2. VolSync wait `--for=condition=Reconciled` 無効 → `latestMoverStatus.result` ポーリングに修正
      3. k3d テストクラスターが本番 HOS パスにバックアップを書き込み recovery を汚染 → Step7.5 でバックアップセクション削除し再発防止
      4. Step6 `kubectl wait --for=delete` ループで ArgoCD が即時再作成するとハングする問題 → wait ループ削除
    - **メモ**: `firstRecoverabilityPoint` は backup patch 後は N/A になる（k3d でのみ発生）
  - _Requirements: 05_
  - _Depends: 4_

- [ ] 7.2 intrusion-response.yml の E2E 実行を確認する
  - `workflow_dispatch` で `namespace=prod` を指定して発火し、forensics Artifacts 生成・NetworkPolicy 適用・Discord 通知（ローテーション一覧含む）の一連の流れを確認する
  - 確認できれば完了
  - _Requirements: 04-2_
  - _Depends: 5.1_
