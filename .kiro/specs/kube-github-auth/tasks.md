# Implementation Plan

本計画は design.md の移行段階 P0〜P7 に沿って並べる。大タスク 1 が P0、2 が P1、3 が P2、4 が P3、5 が P4、6 が P5、7 が P6、8 が P7 に対応する。

- **PR の分割とマージ順**: 大タスク 1 は既存の PR #288 → PR #287 の順にマージする。本仕様の変更は #287 のマージ後に、大タスク 2〜7 をそれぞれ 1 つの PR としてこの順にマージする。本仕様の k3s-server ロール変更は #288 の待機タスクとマニフェスト適用タスクを前提にする。
- **本番に触れる工程**: 【ユーザー作業】はユーザーが自分で行う工程、【本番・ユーザー承認】はユーザーの承認を得てから行うか、ユーザーが実行する工程を表す。admin 権限でのマージ、playbook の実行、Infisical の変更はユーザーが行う。Ansible の出力はファイルへリダイレクトする。
- **main へのマージで本番が変わる経路**: kube-access の RBAC は ArgoCD がマージ後に自動で同期する。k3s-server ロールの変更はマージ時点では反映されないが、その後の k3s-upgrade・DR・k3s-bootstrap のどの実行でも適用される。
- **公開リポジトリへの配慮 (13.5)**: タスクの成果物、コミット、PR 本文、検証記録に、本番の件数 (管理者数・発行数等)、シークレット値、トークン、内部 IP、tailnet 名を書かない。組織・リポジトリ・GitHub ユーザーの数値 ID は GitHub API で誰でも取得できる公開情報なので、設定値として置いてよい。ただし検証記録やログに実値を転記しない。
- **SSH**: 平常時の kubectl に SSH は使わない。例外は 8.3 の client CA forced rotation の作業中だけとする (D17)。

---

- [x] 1. P0: 進行中 PR との Infisical 認証情報の整合とマージ

- [x] 1.1 PR #288 (k3s-bootstrap 冪等化) から作成できない認証情報の前提と kubeconfig 登録を外す
  - kubeconfig を取得して Infisical へ登録する Play を削除する
  - `OPS_INFISICAL_*`・`ESO_INFISICAL_*` の参照を既存の `INFISICAL_CLIENT_*` (K3s identity、Viewer) に戻す。ESO 用 `infisical-auth` の作成と修復にも同じ値を使う
  - CLAUDE.md 等に追記された `OPS_*` の記述を外す
  - Infisical への書込を必要とする処理が残っていないことを確認する
  - 完了状態: PR #288 のブランチで `OPS_*`・`ESO_*` と kubeconfig 登録への参照が検索で 0 件になり、PR の既存の検証が通る
  - _Requirements: 8.5, 10.1, 10.2, 10.3, 10.4, 13.4_

- [x] 1.2 (P) PR #287 (DR 手動承認化) から作成できない認証情報の前提を外し、kubeconfig 取得を読取だけにする
  - `OPS_INFISICAL_*`・`ESO_INFISICAL_*` の参照を `INFISICAL_CLIENT_*` に戻す
  - kubeconfig の再取得処理を、Viewer で読める既存の共有 kubeconfig を読むだけの処理にする。OIDC による生成への置き換えは 6.2 で行う
  - 電源投入だけの DR 経路が、既存の共有 kubeconfig でこれまでどおり動くことを保つ
  - 完了状態: PR #287 のブランチで `OPS_*`・`ESO_*` への参照が 0 件になり、Infisical への書込処理がなく、PR の既存の検証が通る
  - _Requirements: 9.5, 10.1, 10.2, 10.4_
  - _Boundary: PrAlignment (PR #287)_

- [x] 1.3 【ユーザー作業】GitHub Environment `dr-recovery` を手動で作る
  - ユーザーが `gh api` で作成済み。Terraform では管理しない (D9)
  - 保護設定: required reviewers は team `infra` (org のチーム、リポジトリの read 権限を付与済み)、自己承認は許可 (起動者本人が承認できる。別メンバーの承認を求めない方針は D1 と同じ)、管理者 bypass は無効、deployment branch は main のみ
  - team `infra` へのメンバー追加は手動で行う (org の全員を順次追加する予定)。保護設定の内容と追加手順は 7.3 で tech.md に記載する
  - 完了状態: リポジトリの設定に `dr-recovery` があり、上の保護設定が有効になっている
  - _Requirements: 1.6_

- [x] 1.4 EnvironmentGuard が検査する Environments API の応答を確認する (D16)
  - 作成済みの `dr-recovery` を読み取り、required reviewers の規則 (team の reviewer、自己承認の可否)、管理者 bypass、deployment branch policy (main の 1 件だけ) のフィールド名と値の形を確認した。結果は research.md に実値の ID を含めずに記録した
  - #287 の検査ステップは required reviewers の有無だけを見ているため、残りの項目の検査は 6.2 で加える。自己承認の可否は検査しない
  - `environment` claim は `dr-recovery` が main 限定で一時ブランチから参照できないため、DR の OIDC 移行のマージ後に 6.9 で確認する
  - 完了状態: EnvironmentGuard が使うフィールド名が確定し、design.md と research.md に反映されている
  - _Requirements: 1.6, 13.5_

- [x] 1.5 【本番・ユーザー承認】PR #288 → PR #287 の順にマージし、本仕様のブランチを追従させる
  - 前提条件: 1.1・1.2 の検証が通っている
  - マージはユーザーが行う。#288 のマージ後に #287 を main に追従させてからマージする
  - 本仕様のブランチを、両 PR を含む main に追従させる
  - 確認項目: 両 PR の既存の検証、マージ後の main で `OPS_*`・`ESO_*` と kubeconfig 登録の参照がないこと
  - ロールバック: 各 PR を revert する
  - 完了状態: main に両 PR が入り、本仕様のブランチでマニフェスト適用タスクと再起動後の待機タスクを使える
  - _Requirements: 8.5, 10.1, 10.2, 10.3_

- [x] 2. P1: kube-access による RBAC の定義

- [x] 2.1 RBAC の正本として `kube-access` Application を追加する
  - sync-wave -1、prune と selfHeal を有効にし、kube-access のマニフェストのディレクトリを管理させる
  - binding を Git から削除すると、同期によってクラスタからも削除されるようにする
  - 完了状態: Application の定義が既存の App of Apps に読み込まれる位置にあり、マニフェストの静的検査 (既存の lint) が通る
  - _Requirements: 2.8, 5.3_

- [x] 2.2 (P) ワークフロー用の最小権限 ClusterRole と binding を定義する
  - `gha:infra-health-check`: CNPG の Cluster の get/list (全 namespace) と `nodes/stats` の get だけ。`nodes/proxy` は与えない
  - `gha:intrusion-response`: Pod・Pod のログ・Event・NetworkPolicy の get/list と、NetworkPolicy の create だけ
  - `gha:kube-cert-issue`: CSR の create/get、CSR の approval の update、`kubernetes.io/kube-apiserver-client` signer に限った approve だけ。delete は与えない
  - 各ロールに、用途と最小権限の根拠を日本語のコメントで書く
  - k3s-upgrade 用の binding は作らない
  - 完了状態: 3 ユーザー分のロールと binding が定義され、上記以外の verb やリソースを含まないことをレビューで確認できる
  - _Requirements: 2.3, 2.4, 2.5, 2.6, 2.7, 2.8_
  - _Boundary: KubeAccessRbac (workflows, kube-cert-issue)_

- [x] 2.3 (P) DR 復旧ワークフロー用の binding を独立したマニフェストとして定義する
  - `gha:dr-recovery` に組み込みの `cluster-admin` を割り当てる。Environment `dr-recovery` の承認で保護することをコメントに書く
  - bootstrap 時に Ansible が同じ内容を適用する (6.3)。値の複製を避けるため、他の RBAC とファイルを分け、Ansible からはこのファイルだけを参照する
  - 完了状態: DR 用の binding が単独のマニフェストとして存在し、kube-access Application の管理対象に含まれる
  - _Requirements: 2.6, 2.8_
  - _Boundary: DrBootstrapBinding (manifest), KubeAccessRbac_

- [x] 2.4 (P) 人用の binding の形式を定め、初期の binding を用意する
  - 人の binding は `github:<GitHubユーザー名>:<数値ID>` に対する ClusterRoleBinding だけにする。ロールは組み込みの `cluster-admin`・`admin`・`edit`・`view` から選ぶ
  - 初期の binding の対象とロールはユーザーが決める。少なくとも 5.5 の検証を行う担当者の binding を含める
  - PR 本文とコミットに対象者の人数を書かない
  - 完了状態: 人用の binding のマニフェストが形式どおりに存在し、ユーザーが決めた初期の対象が含まれる
  - _Requirements: 5.1, 5.2, 5.6, 12.1, 13.5_
  - _Boundary: KubeAccessRbac (humans)_

- [x] 2.5 【本番・ユーザー承認】RBAC の PR をマージし、ArgoCD での同期を確認する
  - 前提条件: 1.5 が完了している。この時点では `gha:*`・`github:*` のユーザー名は認証されないため、binding は無害である
  - 確認項目: `kube-access` が Synced かつ Healthy で、ClusterRole と binding がクラスタに存在する。既存の Application に影響がない
  - ロールバック: PR を revert し、Application を削除する
  - 完了状態: ArgoCD 上で `kube-access` の同期が成功している
  - _Requirements: 2.8, 9.1_

- [x] 3. P2: 認証設定・署名期間上限・監査ログのノード構成

- [x] 3.1 k3s-server ロールに GitHub OIDC 認証と証明書・監査ログの変数を定義する
  - audience、組織とリポジトリの数値 ID (GitHub API で取得する公開情報)、ワークフローごとの許可イベントと Environment の対応表、クライアント証明書の署名期間上限 (168h)、監査ログのローテーション上限を定義する
  - ワークフローの対応表の初期値は design.md のワークフローポリシー表のとおりにする。infra-health-check は schedule と workflow_dispatch、intrusion-response と kube-cert-issue は workflow_dispatch、dr-recovery は workflow_dispatch と Environment `dr-recovery`。k3s-upgrade と pull_request 系のイベントは含めない
  - 監査ログの上限は、(世代数 + 1) × 1 ファイルの最大サイズがノードのディスク予算に収まる値にする
  - 許可リストが空のとき、テンプレートの描画が失敗するようにする
  - 完了状態: 変数がロールの既定値として定義され、許可リストを空にするとテンプレートの描画が失敗する
  - _Requirements: 1.2, 1.5, 1.6, 2.6, 3.7, 6.1, 12.3, 13.5_

- [x] 3.2 (P) GitHub Actions OIDC を検証する認証設定を作る
  - 匿名認証を明示的に無効にする
  - 発行者を GitHub Actions、audience を 3.1 の値とする JWT authenticator を 1 つだけ置く
  - claim の照合規則を、組織 ID、リポジトリ ID、ref = main、許可リスト上のワークフローファイル (main 上のもの)、ワークフローごとの許可イベント、Environment 指定時の environment claim の一致、github-hosted ランナーとする。規則ごとに拒否理由のメッセージを付ける
  - 存在しない claim は optional 参照で空として扱い、照合が偽になるようにする
  - ユーザー名は `gha:` とワークフローファイル名にする。group と extra は付けない。ユーザー名が `gha:` で始まることを検証する規則を置く
  - k3s のクライアント証明書、ServiceAccount トークン、bootstrap token の認証は既定のまま残す
  - 認証の仲介アプリと Zitadel は使わない
  - 完了状態: 3.1 の変数から認証設定が描画され、design.md の照合規則表の各行に対応する規則とメッセージがそろっている
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.1, 2.2, 6.5, 11.3, 12.1, 13.1, 13.3_
  - _Boundary: GitHubOidcAuthenticator_

- [x] 3.3 (P) 監査ポリシーと、kube-apiserver・kube-controller-manager の起動引数を追加する
  - 監査ポリシーは design.md の検証済みの内容 (Metadata レベル。システムコンポーネント、kube-system の ServiceAccount、ServiceAccount の read、ヘルスチェック系、events、leases を除外) にする
  - kube-apiserver に、認証設定ファイル、監査ポリシー、監査ログの出力先、ローテーション上限を渡す
  - kube-controller-manager に、署名期間の上限 168h を渡す
  - 完了状態: k3s の設定テンプレートを描画すると、認証設定・監査・署名期間の引数が 3.1 の変数の値で出力される
  - _Requirements: 3.7, 11.1, 12.3_
  - _Boundary: AuthnConfigDistribution (k3s config, audit policy)_

- [x] 3.4 認証設定の配布、1 回だけの再起動、起動確認、自動ロールバックをロールに組み込む
  - 認証設定と監査ポリシーを、root のみが読める権限で、k3s のインストールより前に置く。新規構築 (DR) では k3s の初回起動から有効になる
  - 内容に変更がない場合は何も変えず、k3s を再起動しない。変更がある場合は handler で 1 回だけ再起動する
  - 配布前に旧ファイルを退避し、配布・再起動・確認を 1 つのブロックにまとめる。確認は、ノード上のローカル admin で `/readyz` が期限内 (既定 120 秒) に ok を返すことと、匿名の `/version` が 401 になること (設定が読み込まれたことの証明) の両方とする
  - 確認に失敗したら、退避した旧ファイルを戻して再起動し、`/readyz` を確認したうえで playbook を失敗終了させる。旧設定でも復帰しない場合は、その旨を明示して失敗終了する。旧ファイルがない新規構築では失敗終了だけを行う
  - k3s の終了コードや再起動コマンドの戻り値は判定に使わない (不正な設定でも終了コードが 0 になるため)
  - 確認は、PR #288 の etcd 健全性待ちと Ready 待ちより前に行う
  - 完了状態: ロールの構文検査と ansible-lint が通り、確認失敗時に旧設定へ戻すロールバックの処理がロールに含まれている
  - _Depends: 1.5_
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.6_

- [x] 3.5 使い捨ての k3s (本番と同じ v1.36.3) で、描画した認証設定の判定を検証する
  - ロールが描画した認証設定と起動引数で起動し、匿名要求が 401、ノード上のローカル admin (x509) が通ることを確認する
  - OIDC 発行者のモックと自前で署名したトークンで、許可されたワークフロー、各規則の違反 (他の組織・リポジトリ・フォーク、main 以外の ref、許可リストにないワークフロー、pull_request 系イベント、自己ホストランナー、dr-recovery で environment がない、audience だけが一致する) を判定させ、許可されたトークンだけが `gha:` のユーザー名で通ることを確認する
  - 発行者に到達できない状態でも起動し、x509 が通ることを確認する
  - 壊した設定で起動が失敗し、ロールの起動確認が失敗として検知することを確認する。systemd 上の再起動の挙動は KVM の DR テスト環境で 6.7 に確認する
  - 実トークンと数値 ID の実値を記録に残さない。検証後はコンテナと一時ファイルを削除する
  - 完了状態: 許可・拒否の判定がすべて期待どおりで、結果が記録されている
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.1, 2.2, 6.5, 6.6, 11.1, 11.3, 13.5_

- [x] 3.6 使い捨ての k3s で、署名期間の上限と監査ログを検証する
  - CSR を作成・承認すると client CA で署名され、有効期限が 168h で切り詰められることを確認する
  - organization `system:masters` を含む CSR が作成時に拒否されることを確認する
  - 監査ログに `gha:*`・`github:*`・ローカル admin の要求が、ユーザー名・操作・対象リソースとともに記録されることを確認する。システムコンポーネントの要求が記録されないこと、ログの総量がローテーション上限で頭打ちになることも確認する
  - 完了状態: 署名期間・拒否・監査ログの 3 点が期待どおりであることが記録されている
  - _Requirements: 3.7, 12.3_

- [x] 3.7 【本番・ユーザー承認】ノード構成の PR をマージし、ユーザーが k3s-bootstrap を実行して認証設定を本番に入れる
  - 前提条件: 2.5・3.5・3.6 が完了し、CNPG のバックアップが正常で、k3s の再起動 1 回を許容できる時間帯である。マージ後は k3s-upgrade と DR の実行でもこの変更が適用されることを周知する
  - 実行: ユーザーが playbook を実行する (出力はファイルへリダイレクトする)
  - 確認項目: `/readyz` が ok、匿名の `/version` が 401、共有 kubeconfig と `make kubectl` がこれまでどおり使える、GitHub の JWKS 取得が成功していることをメトリクスで確認、2 回目の実行で変更が 0 件になり再起動されない、監査ログの実際の書込量がディスク予算に収まる
  - ロールバック: 起動確認に失敗した場合はロールが自動で旧設定に戻す。動作に問題がある場合は、テンプレートから認証設定の指定を外して再実行する。共有 kubeconfig は 7.4 まで残るため、従来の方式で運用を続けられる
  - 完了状態: 本番で上記の確認項目がすべて満たされ、記録されている
  - _Requirements: 6.1, 6.2, 6.3, 6.5, 9.1, 9.5, 12.3_

- [ ] 4. P3: CI 共通の OIDC 部品と infra-health-check の移行

- [x] 4.1 CI・DR が共通で使う OIDC kubeconfig 生成部品を作る
  - kubeconfig を生成するモード: 接続先 (既定は MagicDNS 名 `prod-node-1`) から server CA を tailnet 経由で取得し、取得した CA で同じエンドポイントの TLS 検証が通ることを確認する。そのうえで、所有者だけが読める kubeconfig に、exec プラグインでトークンを得るユーザーを書き出す。TLS 検証は無効にしない
  - トークンを返すモード: audience を 3.1 と同じ固定値にして GitHub OIDC トークンを要求し、有効期限付きの ExecCredential を返す
  - CA が取得できない、CA と提示された証明書が一致しない、トークンを要求するための環境変数がない、のいずれかの場合は非 0 で終了する
  - トークンをログとファイルに書かない
  - 完了状態: shellcheck が通る。3.5 の使い捨て k3s とトークン要求エンドポイントのモックを使って、生成した kubeconfig で kubectl が `gha:` ユーザーとして認証され、CA を取得できないときは非 0 で終了する
  - _Requirements: 1.9, 7.1, 7.2, 7.4, 13.5_

- [x] 4.2 infra-health-check を OIDC 認証に移行する
  - ジョブに `id-token: write` を与え、4.1 の部品で kubeconfig を生成する。共有 kubeconfig は取得しない
  - CNPG の状態は kubectl で取得する。ディスク使用率は kubelet の stats summary を OIDC トークンで直接取得する
  - Discord webhook の Infisical からの読取 (Viewer) は残す
  - 完了状態: ワークフローとスクリプトから共有 kubeconfig への参照がなくなり、監視項目と通知の内容は従来と同じになっている
  - _Requirements: 1.9, 2.3, 8.1_

- [ ] 4.3 【本番・ユーザー承認】PR をマージし、本番の段階検証 1・2 を行う
  - 前提条件: 3.7 が完了している
  - 確認項目: main での手動実行と schedule 実行が成功し、従来と同じ監視結果が出る。同じワークフローを feature ブランチから dispatch すると 401 になる。許可リストにないワークフローのトークンが 401 になる (一時ワークフローは確認後にブランチと run を削除する)。拒否理由が API サーバーのログに規則のメッセージとして出る
  - D16 の確認: schedule 実行と workflow_dispatch 実行の `event_name` が許可イベントの値と一致する。kubelet への到達で Tailscale ACL の前提 (D14) が成り立っている
  - ロールバック: ワークフローを PR の revert で従来の取得方式に戻す
  - 完了状態: 許可される経路と拒否される経路の両方が本番で確認され、記録されている
  - _Requirements: 1.3, 1.4, 1.5, 8.1, 9.1, 9.5_

- [ ] 5. P4: 人向け証明書の発行と手元のコンテキスト作成

- [x] 5.1 CSR の検証部品とテストを作る
  - CSR が 1 つの PKCS#10 で、自己署名が検証できることを確認する
  - subject が CN 1 属性だけで、期待するユーザー名 `github:<ユーザー名>:<数値ID>` と完全に一致することを確認する
  - organization を含む他の属性や、拡張要求 (SAN、keyUsage 等) を含む CSR を拒否する。属性や拡張の有無は、テキスト出力の見出しではなく ASN.1 の構造の中身で判定する
  - 鍵は EC P-256 以上または RSA 2048 以上に限る
  - 失敗時は理由を 1 行で出し、非 0 で終了する
  - テストには、正常、他人の CN、数値 ID が違う CN、organization 付き、SAN 付き、CN なし、弱い鍵、不正な PEM、PEM が複数、のケースを含める。既存のテストスクリプトと同じ形式にする
  - 完了状態: テストスクリプトがすべてのケースで期待どおりの結果になる
  - _Requirements: 3.3, 3.4, 3.5, 3.6, 5.6, 12.1_

- [x] 5.2 人向け証明書の発行ワークフローを作る
  - workflow_dispatch で起動し、Environment は参照しない。権限は `id-token: write` と `contents: read` だけにする
  - CSR の入力は環境変数経由でスクリプトに渡し、shell に直接展開しない。期待するユーザー名は github コンテキストの起動者から組み立て、入力からは受け取らない
  - 5.1 で検証した後、4.1 の部品で kubeconfig を作る。CSR オブジェクトを、名前に起動者の数値 ID と run ID を含め、クライアント認証用の signer・用途・有効期限の上限 (7 日) で作成し、承認して、署名済み証明書を待つ
  - 証明書だけを artifact にし、保持期間を 1 日にする。job summary に起動者、ユーザー名、証明書の notAfter を記録する。拒否した場合は、理由を job summary とエラー注釈に出し、CSR オブジェクトを作らない
  - CSR オブジェクトは GC で削除される前提にし、削除権限は使わない。発行で RBAC binding は作らない
  - 使うアクションは checkout と upload-artifact だけにし、commit SHA で固定する。トークンと秘密鍵を出力しない
  - 完了状態: ワークフローの静的検査 (actionlint 等の既存の検査) が通り、上記の入出力と権限で定義されている
  - _Depends: 4.1, 5.1_
  - _Requirements: 2.5, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 5.4, 5.5, 12.2, 13.5_

- [ ] 5.3 (P) 手元の login コマンドを作り、`make kubectl` を手元のコンテキストで動くようにする
  - gh で自分のユーザー名と数値 ID を取得し、秘密鍵 (所有者だけが読める権限) と CSR を手元で作る。利用者の openssl 設定に依存しないよう、最小の設定を明示する
  - 発行ワークフローを起動して run を特定し、完了を待って証明書を受け取る。証明書の公開鍵が手元の鍵と一致し、CN が期待どおりであることを確認する
  - server CA を tailnet 経由で取得する。既存のコンテキストの CA と違う場合は指紋を表示して止め、明示的に受け入れるオプションを付けたときだけ更新する
  - すべての確認が成功した後でだけ、コンテキスト `aramakisai-prod` を作成または更新する。途中で失敗した場合は、失敗を表示して既存のエントリを変更しない。再実行すると新しい鍵で再発行してエントリを置き換える
  - 秘密鍵を手元から外へ送らない
  - `make kubectl` を、Infisical を参照せずにコンテキスト `aramakisai-prod` で kubectl を実行する形に置き換える。`make kube-login` を追加し、`make setup` の案内に gh の最低バージョンを追記する
  - 完了状態: shellcheck が通る。発行ワークフローのモック (成功・拒否・タイムアウト) を使った手元の試験で、成功時はコンテキストが作られ、失敗時と CA が違うときは既存の kubeconfig が変わらない
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.6, 7.1, 7.2, 7.3, 7.5, 13.2_
  - _Boundary: KubeLogin / MakeKubectl_

- [ ] 5.4 【本番・ユーザー承認】PR をマージし、発行から kubectl 操作までと権限の剥奪を本番で検証する
  - 前提条件: 2.5・4.3 が完了し、2.4 で検証担当者の binding が同期されている
  - D16 の確認: `make kube-login` で、dispatch による run ID の返却、完了待ち、artifact の取得が設計どおりに動く
  - 確認項目: 承認待ちなしで発行が完了する。発行された証明書で exec、logs のフォロー、port-forward が binding の範囲で動く。job summary に起動者・ユーザー名・有効期限が残る。ログと artifact にトークンと秘密鍵が出ていない。他人の CN の CSR が理由付きで拒否される。re-run しても証明書は最初の起動者の鍵にしか対応しない。CSR オブジェクトが承認から 1〜1.5 時間後に消える
  - 剥奪の確認: binding を削除する PR がマージされ ArgoCD が同期した後、同じ証明書で 403 になる。確認後に binding を戻す
  - write 権限のないアカウントで起動できないことは、可能なら確認する。確認できない場合は GitHub の仕様に依拠していると記録する
  - ロールバック: 共有 kubeconfig は 7.4 まで残るため、Infisical 経由の kubectl で運用を続けられる
  - 完了状態: 上記の確認項目が本番で満たされ、実値を含まない形で記録されている
  - _Requirements: 3.1, 3.4, 3.8, 3.9, 4.1, 4.5, 5.2, 5.3, 9.5, 12.2_

- [ ] 6. P5: 残りの消費者 (intrusion-response・DR・Zitadel・k3s-upgrade) の移行

- [ ] 6.1 (P) intrusion-response を OIDC 認証と tag:ci の tailnet 参加に移行する
  - 両方のジョブに `id-token: write` を与え、4.1 の部品で kubeconfig を生成する
  - tailnet への参加を、他の CI と同じ OAuth と `tag:ci` による方式にする。Infisical からの kube 資格情報と Tailscale API キーの取得、名前解決の手動追記を削除する
  - 隔離の NetworkPolicy は create で作り、既に存在する場合は成功として扱う
  - 完了状態: ワークフローに共有 kubeconfig、Infisical の kube 資格情報、Tailscale API キーへの参照がなく、フォレンジック採取と隔離の手順は従来と同じになっている
  - _Requirements: 1.9, 2.4, 8.2_
  - _Boundary: IntrusionResponseMigration_

- [ ] 6.2 (P) DR 復旧ワークフローと復旧スクリプトを OIDC 認証に移行する
  - ワークフローに `id-token: write` を与え、Environment `dr-recovery` で実行する。EnvironmentGuard が、1.4 で確認したフィールド名で保護設定 (required reviewers が 1 件以上、管理者 bypass が無効、deployment branch が main だけ) を検査し、満たさなければ DR を開始しない。自己承認の可否は検査しない
  - 復旧の開始時と k3s-bootstrap の後に、4.1 の部品で kubeconfig を生成する。クラスタの再作成で CA が変わるため、bootstrap 後に作り直す
  - 旧クラスタの生存確認では、CA が取得できない場合と kubectl が失敗した場合を停止扱いにする
  - 共有 kubeconfig の取得と、Infisical のトークン取得のうち kube 用のものを削除する。Infisical 認証には `INFISICAL_CLIENT_*` だけを使う
  - 完了状態: DR のワークフローとスクリプトに共有 kubeconfig への参照がなく、kubeconfig の生成が開始時と bootstrap 後の 2 か所で行われる
  - _Requirements: 1.6, 1.9, 7.2, 7.4, 8.4, 10.1_
  - _Boundary: DrMigration, EnvironmentGuard_

- [ ] 6.3 (P) k3s-bootstrap で DR 用の binding を ArgoCD の同期より前に適用する
  - ArgoCD の bootstrap Play で、2.3 のマニフェストを PR #288 のマニフェスト適用タスク (server-side apply) で適用する。playbook の完了前に適用されるようにする
  - 値を Ansible 側に複製せず、main の同じファイルだけを参照する
  - 既存クラスタで再実行しても差分が出ないようにする
  - 完了状態: playbook の構文検査と ansible-lint が通り、新規構築の流れの中で DR 用の binding が ArgoCD の同期を待たずに作られる
  - _Requirements: 2.6, 2.8, 6.4, 8.4_
  - _Boundary: DrBootstrapBinding_

- [ ] 6.4 (P) Zitadel のブートストラップとカットオーバーを、共有 kubeconfig に依存しない形にする
  - 環境変数の KUBECONFIG の中身をファイルに書き出す処理を削除する
  - kubectl は標準の kubeconfig の解決と、コンテキストを指定する変数 (既定は `aramakisai-prod`) を使う
  - k3d 検証用の kubeconfig のパス指定は残す
  - 完了状態: ロールに共有 kubeconfig の中身を扱う処理がなく、ansible-lint が通る
  - _Requirements: 8.6_
  - _Boundary: ZitadelBootstrapMigration_

- [ ] 6.5 (P) k3s-upgrade から kube 資格情報と kubectl を外す
  - kubectl のインストールと kube 資格情報の受け渡しを削除する
  - アップグレード前後の状態確認は、k3s-server ロールの etcd 健全性待ちと Ready 待ち (ノード上のローカル admin) に任せる
  - 完了状態: ワークフローに kube 資格情報と kubectl への参照がなく、OIDC の許可リストにも含まれていない
  - _Requirements: 2.6, 8.3_
  - _Boundary: K3sUpgradeMigration_

- [ ] 6.6 移行した消費者をまとめて静的に検証し、1 つの PR にする
  - ワークフローの静的検査、shellcheck、ansible-lint、既存のテストスクリプトが通ることを確認する
  - 1.9 の観点で、CI・DR が Infisical やリポジトリシークレットの kube 資格情報を参照していないことを検索で確認する
  - 完了状態: すべての検査が通り、P5 の変更が 1 つの PR にまとまっている
  - _Depends: 6.1, 6.2, 6.3, 6.4, 6.5_
  - _Requirements: 1.9, 8.2, 8.3, 8.4, 8.6_

- [ ] 6.7 KVM の DR テスト環境で、DR の認証とノード構成の未検証事項を確かめる
  - 本番のクラスタは作り直さない
  - クラスタの再作成後、k3s の初回起動から認証設定が有効で、DR 用の binding が ArgoCD の同期前に存在し、bootstrap 後に作り直した kubeconfig で新しい CA に接続できることを確認する
  - D16 の確認: systemd 上で、不正な認証設定による再起動の挙動を確かめ、ロールの起動確認と自動ロールバックが期待どおりに働くことを確認する。ArgoCD が Ansible の作った binding を引き継ぎ、prune や selfHeal で問題が起きないことも確認する
  - 完了状態: DR の流れが共有 kubeconfig なしで最後まで進み、未検証事項の結果が記録されている。結果が設計と違う場合は差し戻す
  - _Depends: 6.6_
  - _Requirements: 6.4, 6.6, 7.2, 7.4, 8.4_

- [ ] 6.8 【本番・ユーザー承認】PR をマージし、本番で各消費者の動作を確認する
  - 前提条件: 5.4・6.7 が完了している。intrusion-response は隔離と Discord 通知を伴うため、影響のない検証用の対象 (namespace の用意方法を含む) をユーザーと決めてから実行する
  - 確認項目: intrusion-response が tag:ci で tailnet に参加し、フォレンジック採取と NetworkPolicy の作成ができる。k3s-upgrade が kube 資格情報なしで状態確認まで完了する (実行の要否と時期はユーザーが決める)。Zitadel のブートストラップを、ユーザーが手元のコンテキストで実行して成功する。DR の電源投入経路の確認方法はユーザーと決める
  - 検証で作った NetworkPolicy などの後始末は Git 経由で行い、クラスタを直接操作しない
  - ロールバック: 消費者ごとに PR の該当部分を revert して従来の方式に戻す (共有 kubeconfig は 7.4 まで残る)
  - 完了状態: すべての消費者が新しい方式で動くことが本番で確認され、記録されている
  - _Requirements: 8.2, 8.3, 8.4, 8.6, 9.1, 9.5_

- [ ] 6.9 【本番・ユーザー承認】main 上の `dr-recovery` を使い、生存確認ゲートで停止する経路で `environment` claim を確認する (D16)
  - 前提条件: 6.8 が完了し、DR の OIDC 移行が main に入っている。実行すると Issue への記録と失敗通知が出るため、事前に関係者へ知らせる
  - 実行: `dr-recovery` を main から dispatch する。`target_node` は稼働中のノード、`force` は false にする (true にしない)。承認は team `infra` のメンバー (起動者本人でよい) が行う
  - 安全性: 復旧スクリプトは読み取りだけの生存確認ゲートでシグナルを集める。稼働中のノードでは Hetzner・Tailscale・エンドポイントのシグナルが alive になるため、kubectl の結果にかかわらずゲートで停止し、Terraform・Ansible などの破壊的な処理には進まない
  - 確認項目: ゲートのログで `kubectl=alive` になっている (`environment` claim を含む照合で `gha:dr-recovery` が認証された証明)。監査ログに `gha:dr-recovery` の要求が記録されている。`kubectl=dead` の場合は、API サーバーのログで拒否された規則のメッセージを確認し、設計に差し戻す
  - トークンと数値 ID の実値を記録に残さない
  - 完了状態: ゲートで停止した run で `kubectl=alive` が確認され、記録されている
  - _Requirements: 1.6, 8.4, 13.5_

- [ ] 7. P6: 共有 kubeconfig の撤去とドキュメントの同期

- [ ] 7.1 リポジトリから共有 kubeconfig の痕跡を取り除く
  - リポジトリ直下の kubeconfig スタブを削除する。不要になった ignore 設定と gitleaks の除外設定を整理する
  - Infisical の KUBECONFIG を参照するコード、ワークフロー、スクリプトが残っていないことを検索で確認する (k3d 検証用のパス指定と、標準の環境変数としての KUBECONFIG は除く)
  - 完了状態: 共有 kubeconfig を参照する箇所の検索結果が 0 件になる
  - _Requirements: 8.7_

- [ ] 7.2 (P) 運用ドキュメントを新しい認証方式に合わせる
  - CLAUDE.md、README、DR runbook、Zitadel のカットオーバーとロールバックの runbook から、共有 kubeconfig を前提にした記述を除く
  - kubectl の使い方 (`make kube-login` による証明書の発行とコンテキストの作成、7 日ごとの再発行、CA が変わったときの扱い) を書く
  - 権限の付与と剥奪の手順 (binding の追加と削除の PR) と、証明書を失効できないことの注意 (即時の剥奪は binding の削除、残るリスクの上限は有効期限) を書く
  - GitHub 障害時の挙動 (発行、CI、DR は止まる。発行済みの証明書とノード上のローカル admin は使える) と、その間に取れる対応を書く
  - 件数、内部 IP、数値 ID の実値を書かない。経緯や移行の履歴は書かず、現在の仕様だけを書く
  - 完了状態: 対象の文書に共有 kubeconfig の手順が残っておらず、上の 3 項目が記載されている
  - _Requirements: 5.5, 11.2, 11.4, 13.5, 14.1, 14.4_
  - _Boundary: Docs (CLAUDE.md, README, runbooks)_

- [ ] 7.3 (P) steering を新しい認証方式に合わせる
  - tech.md: Infisical のシークレット一覧から KUBECONFIG を除く。kube-access Application を追加した根拠と、bootstrap 時の先行適用 (GitOps 原則の例外) を記録する。発行ワークフローの防御と残存リスク、発行ワークフローと関連スクリプトの変更を cluster-admin 権限の変更と同じ扱いでレビューすること、Environment `dr-recovery` の保護設定の内容 (承認者は team `infra`、起動者本人の承認を認めること、team へのメンバー追加は手動) 、再検証が必要になる条件を記載する
  - dr.md: DR 時の kube-apiserver の認証方式、クラスタを作り直したときの人の証明書の再発行、server CA の扱い、kubectl の実行方法の節を更新する
  - structure.md と vaultwarden-rbac.md: 新しいコンポーネントの置き場所と、共有 kubeconfig を前提にした記述を更新する
  - 値と件数を書かない
  - 完了状態: steering に共有 kubeconfig を前提にした記述がなく、上記の項目が記載されている
  - _Requirements: 11.4, 13.5, 14.1, 14.2, 14.3, 14.4_
  - _Boundary: Docs (steering)_

- [ ] 7.4 【ユーザー作業】PR をマージし、Infisical prod から KUBECONFIG を削除する
  - 前提条件: 3.7・4.3・5.4・6.8・6.9 で、人・CI・DR のすべてが新しい方式で動くことを確認済みで、7.1〜7.3 の PR がマージされている
  - 削除はユーザーが行う (書込可能な machine identity はなく、追加もしない)。値が出力されないよう、CLI の get や一覧は使わないか、出力を捨てる
  - 確認項目: 削除の後、infra-health-check の定期実行、`make kubectl`、発行ワークフローが成功し続ける。Infisical の KUBECONFIG を読もうとして失敗する実行がない
  - ロールバック: 削除後は戻さない。以後の障害は新しい方式で対処する。削除前であれば、どの消費者も従来の方式に戻せる
  - 完了状態: Infisical prod に KUBECONFIG がなく、すべての消費者が正常に動いている
  - _Requirements: 9.1, 9.2, 13.4_

- [ ] 8. P7: client CA の forced rotation による旧共有 admin 証明書の無効化

- [ ] 8.1 KVM の DR テスト環境で forced rotation の手順を通しで確かめる
  - design.md の手順で行う。新しい client CA を正しい置き場所に置き、forced で rotation を実行し、k3s を再起動する
  - 確認項目: 旧 admin 証明書、旧ローカル admin のコピー、人の旧証明書が 401 になる。新しいローカル admin と OIDC 認証が通る。client CA の指紋が変わり、server CA の指紋は変わらない
  - D16 の確認: systemd 上の再起動で、既存のコンテナが残り Pod が落ちない
  - design.md の「rotation の戻し方」を同じ環境で試す。旧 client CA の退避と etcd スナップショットの取得、戻し方 A (API が応答する場合の forced rotate-ca による旧 CA への戻し)、戻し方 B (k3s が起動しない場合の etcd スナップショットからの復元と client CA の差し戻し) を行い、戻した後に client CA の指紋が rotation 前と同じになり、rotation 前の証明書・ローカル admin・OIDC が通り、ノードと Pod が正常であることを確認する
  - 完了状態: 手順、確認項目、戻し方 A・B が KVM の環境で成功し、本番で使うチェックリストとして記録されている。問題が出た場合は本番で実施せず、理由と次に判断する時期を記録する
  - _Depends: 7.4_
  - _Requirements: 9.3, 9.4_

- [ ] 8.2 【ユーザー作業】本番の rotation を実施するかどうかと、メンテナンス時間を決める
  - 実施時期は任意。実施する場合はメンテナンス時間を決め、証明書の再発行が必要になることを利用者に知らせる
  - 実施しない、または延期する場合は、その理由と次に判断する時期を記録する
  - 完了状態: 実施の可否と時期 (または延期の理由と期限) が記録されている
  - _Requirements: 9.3_

- [ ] 8.3 【本番・ユーザー承認】メンテナンス時間に本番の client CA を forced rotation する
  - 前提条件: 7.4・8.1 が完了し、8.2 で実施が決まっている。CNPG のバックアップが正常で、rotation 直前に etcd のスナップショットを取得し、旧 client CA をノード上の戻し用ディレクトリに退避してある (鍵はノードから持ち出さない)
  - SSH 例外 (D17): rotation コマンドの実行開始から、新しい CA での接続 (OIDC と `make kube-login` による再発行) と下記の確認の完了までに限り、SSH でノード上のローカル admin を kubectl と復旧の代わりに使ってよい。目的は、rotation コマンドの実行、再起動後の確認、人の再発行が通らない場合の復旧、下記の戻し方に限る。戻し方を行った場合は、戻した後の確認が終わった時点で例外の期間を終える。完了後は SSH での kubectl に戻さない
  - 確認項目: 旧共有 admin 証明書、旧ローカル admin のコピー、人の旧証明書が 401 になる。ノード上のローカル admin が通る。ノードが Ready で、Pod が稼働し続けている。server CA の指紋が変わらず、client CA の指紋が新しい CA のものになっている。infra-health-check が OIDC で成功する。人が `make kube-login` で再発行でき、kubectl が使える
  - ロールバック: k3s が復帰しない、またはノード・Pod が正常に戻らない場合は、design.md の「rotation の戻し方」で退避した旧 client CA に戻す。API が応答すれば戻し方 A、k3s が起動しなければ戻し方 B (rotation 後の書込を失うため A が使えない場合に限る) を使う。戻した後は、`/readyz` が ok、ノードが Ready で Pod が稼働、client CA の指紋が rotation 前と同じ、server CA の指紋が不変、ローカル admin・OIDC・rotation 前の人の証明書が通ることを確認する。原因を調べてから再実施を改めて判断する
  - 実施記録には指紋や証明書の実値、件数を書かない
  - 完了状態: 上記の確認項目がすべて満たされ、SSH 例外の期間が終わったことが記録されている
  - _Requirements: 7.3, 9.3, 9.4, 13.2, 13.5_
