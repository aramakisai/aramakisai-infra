# Implementation Plan

- [x] 1. Wiki.js用シークレットをInfisicalへ事前登録する
  - DB接続パスワードとAuthentik OIDCクライアントシークレットを`openssl rand -base64 32`等の暗号論的に安全な方法でそれぞれ生成する
  - 生成した値をInfisicalの`WIKIJS_DB_PASSWORD`/`WIKIJS_OIDC_CLIENT_SECRET`として登録する(値を辞書的に推測可能なものにしない、既存パスワードを使い回さない)
  - Discord Ops Webhook通知用の既存キーが利用可能であること(新規登録不要)を確認する
  - 両キーがInfisicalから取得できることを確認する(取得コマンドの出力は値を画面に表示しない形で実行する)
  - _Requirements: 4.3, 4.5_

- [x] 2. Wiki.js関連シークレットをKubernetesへ注入する仕組みを構築する
  - DBパスワード・OIDCクライアントシークレット・Discord Webhook URLの3種をInfisicalからKubernetes Secretへ同期するExternalSecretを定義する
  - 既存サービスと同じClusterSecretStore・リフレッシュ間隔のパターンに従う
  - デプロイ後、対応するKubernetes Secretが生成され3キー全てが含まれることを確認できる状態にする
  - _Requirements: 4.1, 4.2, 4.4_
  - _Depends: 1_

- [x] 3. 外部公開経路とAuthentik OIDC定義をTerraformで構成する
- [x] 3.1 (P) wiki.aramakisai.comへの外部アクセス経路をCloudflare Tunnel経由で構成する
  - 既存の他サービスと同じingress_rule追加パターンでWiki.js Serviceへの直接ルーティングを定義する
  - 対応するDNS CNAMEレコードをトンネル向けに追加する
  - `terraform plan`で意図した差分のみが検出される状態にする
  - _Requirements: 5.1, 5.2_
  - _Boundary: Cloudflare Tunnel設定 (Terraform)_

- [x] 3.2 (P) AuthentikにWiki.js用のOIDC Provider/Applicationを登録する
  - 既存パターンに準拠した新規Provider/Applicationリソースを定義し、Infisicalに登録済みのクライアントシークレットを入力として使用する
  - 既存の共有groupsスコープマッピング(Authentikグループ名をそのままgroupsクレームへ出力するリソース)をこのProviderに含める(新規カスタムマッピングは作らない)
  - `terraform plan`で意図した差分のみが検出される状態にする
  - _Requirements: 3.1, 9.4_
  - _Boundary: Authentik Terraformリソース_
  - _Depends: 1_

- [x] 4. Wiki.js専用のCNPG Postgresクラスタを構築する
- [x] 4.1 専用データベースクラスタを定義する
  - 単一インスタンス構成、リカバリベースのDR方式(bootstrap.recovery)、専用オブジェクトストレージパスへのバックアップ設定、明示的な保持ポリシーを持つCNPG Clusterを定義する
  - バックアップ処理時のメモリスパイクでOOMが発生した場合の対応方針をコメントとして残す
  - クラスタリソースがReadyになる状態を作る
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6_
  - _Boundary: wikijs-db Cluster_
  - _Depends: 2_

- [x] 4.2 日次バックアップスケジュールを構成する
  - 秒付き6フィールドcron形式でスケジュールを定義する(5フィールド形式による誤発火を避ける)
  - スケジュールの次回実行予定が意図した間隔になっている状態にする
  - _Requirements: 2.5_
  - _Boundary: wikijs-db Cluster_
  - _Depends: 4.1_

- [x] 4.3 データベースクラスタをアプリ本体より先に同期させる
  - 専用のArgoCD Applicationとして分離し、アプリ本体より優先して同期されるsync-wave設定を行う
  - ArgoCD上でこのApplicationが独立してSynced/Healthyになる状態にする
  - _Requirements: 2.1_
  - _Depends: 4.2_

- [x] 5. Wiki.js本体を稼働させる
- [x] 5.1 アップロードファイル用の永続ストレージを準備する
  - 既存サービスと同じStorageClassのPersistentVolumeClaimを定義する
  - PVCがBound状態になる状態にする
  - _Requirements: 1.2_
  - _Boundary: Wiki.js Deployment_

- [x] 5.2 Wiki.js本体をデプロイする
  - 固定バージョンタグのイメージを1レプリカで稼働させ、DB接続情報を環境変数経由で受け取る構成にする
  - 実測プロセスメモリを踏まえた明示的なCPU/メモリのrequests・limitsを設定する
  - 着手前に実装対象コンポーネント(DB接続環境変数・config.yml挙動の前提)について公式ドキュメント/ソースコードで前提を再確認し、差異があれば設計を更新してから実装する
  - Pod起動ログでデータベース接続成功が確認できる状態にする
  - _Requirements: 1.1, 1.3, 1.4, 1.5, 6.1, 8.1, 8.2, 8.3_
  - _Boundary: Wiki.js Deployment_
  - _Depends: 2, 4.1, 5.1_

- [x] 6. Authentik OIDCストラテジーの宣言的反映を構築する
- [x] 6.1 認証ストラテジーとグループ権限をデータベースへ書き込むロジックを定義する
  - `authentication`テーブルへの`ON CONFLICT (key) DO UPDATE`によるUPSERTロジックを定義し、OIDC設定一式(client情報・エンドポイント・グループマッピング有効化設定含む)を書き込む
  - `domainWhitelist`/`autoEnrollGroups`を期待される形式でラップする
  - 既存の`local`認証ストラテジー行には触れない
  - Authentikグループ"管理者"/"リーダー"と同名のグループを、Wiki.js組み込みAdministratorsグループと同一の全権限で作成するロジックを定義する(該当グループ名の行が既に存在する場合は何も変更しない)
  - 着手前に対象の内部スキーマ前提について公式ドキュメント/ソースコードで再確認し、差異があれば設計を更新してから実装する
  - _Requirements: 3.3, 3.4, 3.6, 8.1, 8.2, 9.1, 9.2, 9.3_
  - _Boundary: auth-strategy ConfigMap/Job_
  - _Depends: 2_

- [x] 6.2 認証ストラテジー反映ジョブを安全に実行する
  - Infisical経由のシークレットを環境変数として受け取り、UPSERT実行前にデータベースのスキーマ構成が想定と一致するか検証する仕組みを組み込む
  - スキーマ不一致を検知した場合はUPSERTを実行せずDiscord通知の上で失敗終了させる
  - 完了後に自動クリーンアップされる設定を行う
  - 着手前にスキーマ前提の再確認を行い、差異があれば設計を更新してから実装する
  - ジョブ成功時に対象テーブル行が正しい形式で存在する状態にする
  - _Requirements: 3.2, 3.5, 3.8, 3.9, 8.1, 8.2, 8.3_
  - _Boundary: auth-strategy ConfigMap/Job_
  - _Depends: 6.1_

- [x] 6.3 (P) 認証設定・グループ権限反映のためWiki.jsを自動再起動する仕組みを構築する
  - 認証ストラテジー・グループ権限反映ジョブの成功のみをトリガーとしてWiki.js本体を再起動する専用ジョブと、それに必要な最小権限のアクセス制御を定義する
  - 反映ジョブが失敗した場合はこの再起動が実行されない状態にする
  - 再起動トリガー後にWiki.js Podが入れ替わる状態にする
  - _Requirements: 3.7, 9.5_
  - _Boundary: auth-strategy-restart Job/RBAC_
  - _Depends: 5.2_

- [x] 7. Wiki.js一式をArgoCDのデプロイ対象として登録する
  - アプリ本体・認証設定ジョブ群を含むApplicationを定義し、データベースクラスタより後に同期される設定にする
  - ArgoCD上でこのApplicationがSynced/Healthyになる状態にする
  - _Requirements: 1.1_
  - _Depends: 4.3, 5.2, 6.2, 6.3_

- [x] 8. 導入内容をプロジェクトドキュメントへ反映する
- [x] 8.1 (P) デプロイされるサービス一覧にWiki.jsを追加する
  - サービス一覧ドキュメントにWiki.jsのエントリを追加する
  - _Requirements: 7.1_

- [x] 8.2 (P) シークレット一覧ドキュメントを更新する
  - 新規追加したInfisicalシークレットキーをシークレット一覧ドキュメントに追記する(値は含めない)
  - _Requirements: 7.2_

- [x] 9. デプロイ内容を検証する
- [x] 9.1 外部公開経路の疎通を確認する
  - Terraformを適用し、DNS解決とトンネル経由の疎通を確認する
  - _Requirements: 3.1, 5.1, 5.2_
  - _Depends: 3.1, 3.2_

- [x] 9.2 デプロイ全体の起動順序を確認する
  - データベースクラスタがReadyになった後にWiki.js Podが起動し、正常にDB接続できることを確認する
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - _Depends: 7, 9.1_

- [x] 9.3 (P) ノードリソース使用状況を実測する
  - デプロイ前後のノードメモリ使用率を比較し、既存ワークロードへの影響がないことを確認する
  - 実測値がrequests/limitsの想定を大きく超過している場合はリソース制限を見直す
  - _Requirements: 6.1, 6.2, 6.3_
  - _Depends: 9.2_

- [x] 9.4 (P) OIDCログインと認証設定反映を確認する
  - 認証ストラテジー反映ジョブが成功し、Wiki.js Podが自動再起動され、ブラウザからAuthentikアカウントでログインできることを確認する
  - `local`管理者アカウントによるフォールバックログインが引き続き機能することを確認する
  - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 5.1, 5.2_
  - _Depends: 9.2_

- [x] 9.5 スキーマ不一致検知の動作を確認する
  - データベースのスキーマ構成を一時的に想定と異なる状態にした上でジョブを実行し、UPSERTがスキップされ通知が送信され、ジョブが失敗終了することを確認する
  - 確認後にデータベースを原状回復する
  - _Requirements: 3.9_
  - _Depends: 9.4_

- [x] 9.6 (P) バックアップスケジュールの動作を確認する
  - スケジュールされたバックアップの次回実行予定が意図した間隔になっていることを確認する
  - _Requirements: 2.5_
  - _Depends: 9.2_

- [x] 9.7 (P) グループベースの管理者権限付与を確認する
  - Authentikグループ"管理者"または"リーダー"所属アカウントでログインし、Wiki.js管理者ダッシュボードにアクセスできることを確認する
  - 反映ジョブを再実行しても、Admin UIから手動変更した権限が上書きされないことを確認する
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_
  - _Depends: 9.4_
