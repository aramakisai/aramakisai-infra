# Requirements Document

## Project Description (Input)
共有の cluster-admin kubeconfig (Infisical の KUBECONFIG キー) を廃止し、kube-apiserver の認証を GitHub を基準にした方式へ移行する。

背景: 現状は k3s の cluster-admin クライアント証明書入り kubeconfig を Infisical prod の KUBECONFIG に保存し、人 (make kubectl)・CI (infra-health-check, intrusion-response, k3s-upgrade)・DR (recovery.sh)・ansible (k3s-bootstrap Play 5 が書込、zitadel-bootstrap/cutover が読取) が共有している。クラスタ再作成や証明書更新のたびに Infisical へ書き込む必要があるが、Infisical machine identity は K3s (Viewer) 1 つのみでプラン上限のため書込用 identity を作れない。Zitadel を kube OIDC に使うと IdP がクラスタ内にあり循環依存になる。新しいアプリケーション (Dex, Tailscale operator 等) は導入しない。kubectl アクセスに SSH は使わない (ネットワーク経路は従来どおり tailnet 経由で kube-apiserver 6443 に直接接続)。

方針:
- kube-apiserver に structured authentication (AuthenticationConfiguration, --authentication-config) を設定し、GitHub Actions OIDC (token.actions.githubusercontent.com) を直接検証する。claimValidationRules で repository_owner_id / repository_id (数値 ID)、ref == refs/heads/main、job_workflow_ref、event_name、必要に応じ environment を照合する。ユーザー名はワークフローファイル単位にマッピングし、RBAC (gitops 管理) でワークフローごとに最小権限を付与する。CI・DR はこの方式で kubectl を使う。
- 人は k8s CSR API (certificates.k8s.io/v1, signer kubernetes.io/kube-apiserver-client) で個人ごとの短命クライアント証明書 (CN=github:<GitHub ユーザー名>:<GitHub ユーザーの数値 ID>、有効期限 7 日) を取得する。手元で秘密鍵と CSR を作り (秘密鍵は手元から出さない)、gh で発行用 workflow_dispatch ワークフローを起動、ワークフローは GitHub OIDC で認証し CSR の subject が起動者本人のみであることを検証してから CSR を作成・承認し、署名済み証明書を返す。権限は gitops の ClusterRoleBinding (github:<name>:<id>) で付与・剥奪する。
- 認証設定ファイルは ansible k3s-server ロールで配布 (k3s 再起動 1 回)。
- 廃止: Infisical KUBECONFIG、#288 の Play 5 (kubeconfig 登録)、OPS_INFISICAL_* 前提、手元 kubeconfig スタブ。廃止後は k3s のクライアント CA をローテーションする。
- 進行中の PR #288 (k3s-bootstrap 冪等化) と #287 (DR 手動承認化) から OPS_* / ESO_* 前提を外し INFISICAL_CLIENT_* に戻す調整を含む。
- 未決事項は設計フェーズですべて決定済み (末尾の「決定事項」参照)。

## Introduction
本番 K3s クラスタの kube-apiserver へのアクセスは、Infisical prod に保存された共有 cluster-admin kubeconfig に人・CI・DR・Ansible のすべてが依存している。この方式は (1) 漏洩時に失効できない最上位権限の資格情報を全員で共有している、(2) クラスタ再作成や証明書更新のたびに Infisical への書き込みが必要だが書込可能な machine identity を用意できない、(3) 操作者を個人やワークフロー単位で識別できない、という問題を抱える。

本仕様は、kube-apiserver の認証を GitHub を基準にした方式へ移行する。CI と DR は GitHub Actions が発行する OIDC トークンを kube-apiserver が直接検証して認証し、人は GitHub の権限に基づいて発行される個人ごとの短命クライアント証明書で認証する。権限はすべて gitops 管理の RBAC でユーザー単位に最小限だけ付与する。移行完了後、共有 kubeconfig の保存・配布を廃止する。

**Objective:** As a インフラ担当者, I want kube-apiserver への認証を保存された共有資格情報から GitHub のアイデンティティに基づく方式へ移行したい, so that 資格情報の共有・保管・再登録をなくし、操作者を特定でき、権限を Git で付与・剥奪できる

## Boundary Context
- **In scope**:
  - kube-apiserver による GitHub Actions OIDC トークンの直接検証と、その認証設定のノード構成による配布
  - ワークフロー単位・GitHub ユーザー単位のユーザー名と、gitops 管理の RBAC による最小権限付与
  - 人向け短命クライアント証明書の発行ワークフローと、手元 kubeconfig へのコンテキスト作成手段
  - 既存の共有 kubeconfig 消費者 (人の kubectl 実行手段、infra-health-check、intrusion-response、k3s-upgrade、DR 復旧、k3s ブートストラップ、Zitadel ブートストラップ/カットオーバー) の移行
  - 共有 kubeconfig の廃止と、それに含まれていたクライアント証明書の扱いの判断
  - 進行中 PR (k3s-bootstrap 冪等化、DR 手動承認化) との Infisical 認証情報の整合
  - 関連ドキュメントの同期
- **Out of scope**:
  - 認証仲介・プロキシとなる新規アプリケーション (Dex、Tailscale operator 等) の導入
  - SSH を経由した kubectl アクセス (平常時。client CA rotation の作業中に限る例外は Requirement 13.2)
  - Zitadel を kube-apiserver の認証基盤として使うこと
  - Infisical machine identity の追加や書込権限の付与
  - ノード構成のための Ansible の SSH 接続方式の変更
  - kube-apiserver へのネットワーク経路の変更 (従来どおり tailnet 経由で直接接続)
- **Adjacent expectations**:
  - GitHub (Actions OIDC 発行、リポジトリ権限、Environment 承認) が認証の信頼の起点となる。GitHub 障害時の挙動は Requirement 11 のとおり受け入れる
  - ArgoCD が RBAC リソースの正本を管理する
  - 進行中 PR #288 (k3s-bootstrap 冪等化) と PR #287 (DR 手動承認化) は本仕様の決定に合わせて調整される

## Requirements

### Requirement 1: GitHub Actions OIDC トークンの直接検証
**Objective:** As a CI・DR ワークフローの管理者, I want kube-apiserver が GitHub Actions の OIDC トークンを直接検証して認証してほしい, so that 保存されたシークレットに依存せずにワークフローから kubectl を実行できる

#### Acceptance Criteria
1. When GitHub Actions のワークフローが GitHub Actions OIDC 発行者のトークンを提示した, the kube-apiserver shall 発行者の公開鍵による署名検証と有効期限の検証を行い、すべての照合条件を満たす場合に限り認証する
2. The kube-apiserver shall トークンの受け入れ可否を、組織およびリポジトリの不変の数値 ID の照合によって判定し、組織名・リポジトリ名の文字列のみによる照合を受け入れ条件にしない
3. If トークンの ref が main ブランチ以外を示す, then the kube-apiserver shall 認証を拒否する
4. If トークンの発行元ワークフローファイルが許可されたワークフローファイルのいずれとも一致しない, then the kube-apiserver shall 認証を拒否する
5. If トークンのイベント種別がワークフローごとに許可されたイベント種別に含まれない (pull_request 系イベントを含む), then the kube-apiserver shall 認証を拒否する
6. Where ワークフローにクラスタ変更を伴う高権限 (DR 復旧等) が割り当てられている, the kube-apiserver shall トークンが承認必須の GitHub Environment 内で実行されたジョブから発行されたことを確認できた場合に限り認証する。証明書発行ワークフローは本条件の対象外とし、Requirement 3 の検証と Requirement 3.7 の有効期限上限によって保護する
7. The kube-apiserver shall トークンの audience 値のみを受け入れ可否の根拠にしない (audience は要求者が任意に指定できるため、本 Requirement の他の照合条件と組み合わせて判定する)
8. If 他の組織・他のリポジトリ (フォークを含む) のワークフローが発行したトークンが提示された, then the kube-apiserver shall 認証を拒否する
9. The CI・DR ワークフロー shall kube-apiserver への認証に、Infisical やリポジトリシークレットに保存された kube 資格情報を使用しない

### Requirement 2: ワークフロー単位の識別と最小権限
**Objective:** As a インフラ担当者, I want ワークフローファイルごとに別のユーザーとして識別し必要最小限の権限だけを与えたい, so that 1 つのワークフローが侵害されても影響範囲をその用途に限定できる

#### Acceptance Criteria
1. When kube-apiserver が GitHub Actions OIDC トークンを認証した, the kube-apiserver shall 発行元ワークフローファイルごとに一意で、人のユーザー名と衝突しないユーザー名を割り当てる
2. The kube-apiserver shall 認証したワークフローに組織やリポジトリ全体を表すグループ等、ワークフローをまたいで権限を共有させる属性を付与しない
3. The gitops 管理の RBAC shall infra-health-check のユーザーに、ヘルスチェックに必要なリソースの読み取り権限のみを付与し、kubelet 上でのコマンド実行に転用できる権限 (`nodes/proxy`) を付与しない
4. The gitops 管理の RBAC shall intrusion-response のユーザーに、フォレンジック採取に必要なログ・イベント・NetworkPolicy の読み取りと NetworkPolicy の作成のみを許可する
5. The gitops 管理の RBAC shall 証明書発行ワークフローのユーザーに、クライアント証明書署名要求の作成・承認とその結果の取得に必要な権限のみを付与する
6. The gitops 管理の RBAC shall DR 復旧のワークフローに復旧処理に必要なクラスタ管理権限 (cluster-admin 相当) を付与し、k3s アップグレードのワークフローには kube-apiserver の権限を付与しない (OIDC 認証の許可対象に含めない)
7. If RBAC binding が存在しないワークフローのユーザーが API を呼び出した, then the kube-apiserver shall その要求を認可しない
8. The gitops 管理の RBAC shall すべての権限付与を ArgoCD が管理する Git 上のマニフェストとして定義し、クラスタへの直接操作による付与を前提にしない

### Requirement 3: 人向け短命クライアント証明書の発行
**Objective:** As a リポジトリの write 権限を持つ委員会メンバー, I want GitHub の権限に基づいて自分専用の短命なクライアント証明書を発行したい, so that 共有資格情報を使わずに自分のアイデンティティで kubectl を実行できる

#### Acceptance Criteria
1. The 証明書発行ワークフロー shall 利用者が手動で起動するワークフローとして提供され、起動できるのはリポジトリの write 権限保持者に限られる。起動者以外の承認を待たずに発行を完了し、利用者は単独で再発行できる
2. The 証明書発行ワークフロー shall 利用者の手元で生成された署名要求のみを受け付け、秘密鍵を受け取らない・生成しない
3. When 署名要求の subject が起動者本人の GitHub ユーザー名と不変の数値 ID の組に対応するユーザー名 (`github:<GitHubユーザー名>:<数値ID>`) のみで構成されている, the 証明書発行ワークフロー shall クラスタの証明書署名 API に署名要求を作成・承認し、k3s のクライアント CA で署名された証明書を起動者に返す
4. If 署名要求の subject が起動者本人以外のユーザー名を示す, then the 証明書発行ワークフロー shall 発行を拒否し、拒否理由をワークフローの結果として示す
5. If 署名要求の subject にユーザー名以外の属性 (グループを表す organization 等) が含まれる, then the 証明書発行ワークフロー shall 発行を拒否する
6. If 署名要求の形式が不正、または鍵用途がクライアント認証以外を要求している, then the 証明書発行ワークフロー shall 発行を拒否する
7. The 証明書発行ワークフロー shall 発行する証明書の有効期限を、7 日以内に設定し、クラスタ側でもクライアント証明書の署名期間に同じ上限を課す
8. The 証明書発行ワークフロー shall ワークフローログ・成果物に秘密鍵・トークンを出力しない (署名要求と署名済み証明書は公開情報として出力してよい)
9. When 署名要求の処理が完了した, the 証明書発行ワークフロー shall 作成した署名要求オブジェクトがクラスタに無期限に残らないよう扱う

### Requirement 4: 利用者のコンテキスト作成と kubectl 操作
**Objective:** As a 委員会メンバー, I want `argocd login` と同程度の手軽さで手元の kubeconfig にクラスタのコンテキストを作りたい, so that 証明書の仕組みを意識せずに通常の kubectl 操作ができる

#### Acceptance Criteria
1. When 利用者が手元で発行用コマンドを 1 回実行した, the 証明書取得手段 shall 秘密鍵生成・署名要求作成・発行ワークフローの起動・完了待ち・証明書受領・手元 kubeconfig へのコンテキスト作成までを完了する
2. The 証明書取得手段 shall 秘密鍵を利用者の手元から外部へ送信しない
3. When 有効期限切れまたは期限間近の証明書を持つ利用者が発行用コマンドを再実行した, the 証明書取得手段 shall 既存のコンテキストを新しい証明書で更新する
4. If 発行ワークフローが発行を拒否した、または完了しなかった, then the 証明書取得手段 shall 利用者に失敗を示し、手元 kubeconfig の既存コンテキストを壊さない
5. While 利用者が有効な証明書と対応する RBAC binding を持つ, the kube-apiserver shall tailnet 経由の直接接続で、exec・logs のフォロー・port-forward を含む通常の kubectl 操作を binding の権限の範囲で許可する
6. The リポジトリの kubectl 実行手段 (従来 `make kubectl` が担っていた入口) shall 共有 kubeconfig を取得せず、利用者の手元のコンテキストを用いて動作する

### Requirement 5: 人の権限の付与と剥奪
**Objective:** As a インフラ担当者, I want 人の権限を Git の変更だけで付与・剥奪したい, so that 証明書を失効できなくても権限を即座に取り消せ、誰が何をできるかを Git で追跡できる

#### Acceptance Criteria
1. The gitops 管理の RBAC shall 人の権限を GitHub ユーザー名と数値 ID の組に対応するユーザー名 (`github:<GitHubユーザー名>:<数値ID>`) に対する binding としてのみ付与する
2. If 有効な証明書を持つ利用者に対応する RBAC binding が存在しない, then the kube-apiserver shall 認証は成立しても一切の API 操作を認可しない
3. When 利用者の RBAC binding が Git から削除され ArgoCD が同期した, the kube-apiserver shall 当該利用者の以後の要求を、証明書の有効期限内であっても認可しない
4. The 証明書発行ワークフロー shall 発行の可否を RBAC binding の有無と独立に判定し、発行行為そのものによって権限を付与しない
5. The 人の認証方式 shall 発行済み証明書の失効手段を持たないことを前提とし、権限の即時剥奪は binding 削除、残存リスクの上限は有効期限の短さで担保する
6. If GitHub ユーザー名の変更または別人による旧ユーザー名の再取得が起きた, then the kube-apiserver shall 既存の binding によってその証明書の要求を認可しない (数値 ID が一致しないため)

### Requirement 6: 認証設定の配布と DR 直後の可用性
**Objective:** As a インフラ担当者, I want OIDC 認証設定をノード構成の一部として冪等に配布したい, so that 新規構築や DR 直後から kubeconfig なしで CI・DR が kube-apiserver に認証できる

#### Acceptance Criteria
1. The k3s ブートストラップ (k3s-server ロール) shall kube-apiserver の GitHub Actions OIDC 認証設定をノードに配布し、kube-apiserver にその設定を読み込ませる
2. When 認証設定に変更がない状態で k3s ブートストラップを再実行した, the k3s ブートストラップ shall 設定を変更せず k3s を再起動しない
3. When 認証設定に変更がある状態で k3s ブートストラップを実行した, the k3s ブートストラップ shall 設定を更新し、k3s の再起動を 1 回以内に抑えて反映する
4. When DR でクラスタを新規に作り直した, the k3s ブートストラップ shall k3s の起動時点から OIDC 認証が有効な状態にし、DR 復旧処理が共有 kubeconfig なしで後続の kubectl 操作を進められるようにする
5. The kube-apiserver shall OIDC 認証設定の追加後も、k3s 自身のコンポーネントが使う既存の認証手段を引き続き受け付ける
6. If 認証設定の内容が不正で kube-apiserver が起動できない, then the k3s ブートストラップ shall 失敗として検知し、適用前の認証設定へ自動で戻して k3s を起動し直したうえで実行を失敗終了させる

### Requirement 7: server CA 証明書の入手とクラスタ再作成時の挙動
**Objective:** As a kubectl 利用者・ワークフロー, I want kube-apiserver のサーバー証明書を検証するための CA 証明書を共有シークレットなしで入手したい, so that 中間者攻撃を防ぎつつ kubeconfig の配布をなくせる

#### Acceptance Criteria
1. The 証明書取得手段およびワークフロー shall kube-apiserver の server CA 証明書を、Infisical 等に保存されたシークレット・SSH・リポジトリ内の保存値に依存せず、kube-apiserver のノードから tailnet 経由で取得し、取得した CA で接続先の TLS 検証が成立することを確認して用いる。TLS 検証は無効化しない
2. When DR 等でクラスタが再作成され CA が変わった, the 証明書取得手段およびワークフロー shall 新しい server CA 証明書を入手できる
3. When クラスタが再作成されクライアント CA が変わった, the 人の認証方式 shall 既存の人の証明書を無効とし、利用者は発行用コマンドの再実行で再発行できる
4. When クラスタが再作成された, the CI・DR ワークフロー shall 保存済み資格情報の再登録なしに OIDC 認証で kube-apiserver へ接続できる
5. If 取得した server CA 証明書が手元の既存コンテキストの CA と異なる, then the 証明書取得手段 shall 違いを利用者に示し、利用者が明示的に受け入れた場合に限りコンテキストの CA を更新する

### Requirement 8: 既存の共有 kubeconfig 消費者の移行
**Objective:** As a インフラ担当者, I want 共有 kubeconfig を使っているすべての処理を新方式へ移行したい, so that 共有 kubeconfig を安全に廃止できる

#### Acceptance Criteria
1. The infra-health-check ワークフロー shall 読み取り専用のワークフロー用ユーザーとして OIDC 認証で kube-apiserver へ接続し、従来と同じ監視結果を出せる
2. The intrusion-response ワークフロー shall 他の CI と同じタグ付きの方式で tailnet に参加し、Infisical から kube 資格情報や Tailscale API キーを取得せずに、自身のワークフロー用ユーザーとして OIDC 認証で接続し、フォレンジック採取と対象 namespace の隔離を従来どおり実行できる
3. The k3s-upgrade ワークフロー shall kube-apiserver の資格情報を使わずに、アップグレード前後のクラスタ状態確認を従来どおり実行できる
4. The DR 復旧処理 shall OIDC 認証で接続し、共有 kubeconfig なしで復旧の全工程を実行できる
5. The k3s ブートストラップ shall kubeconfig を取得して Infisical へ登録する処理を持たない
6. The Zitadel ブートストラップおよびカットオーバー処理 shall 共有 kubeconfig に依存せずに kube-apiserver の操作を実行できる
7. The 移行後のリポジトリ shall 共有 kubeconfig を参照するコード・ワークフロー・手元用 kubeconfig スタブを含まない

### Requirement 9: 段階的移行と共有 kubeconfig の廃止
**Objective:** As a インフラ担当者, I want kubectl が使えない時間を作らずに移行し、移行後は旧資格情報を無力化できるようにしたい, so that 運用を止めずに漏洩リスクを解消できる

#### Acceptance Criteria
1. The 移行手順 shall 新方式の導入と検証、各消費者の移行、共有 kubeconfig の削除の順に進め、どの時点でも人・CI・DR のいずれかが kube-apiserver に接続できない期間を作らない
2. When すべての消費者の新方式での動作が確認された, the 移行手順 shall Infisical prod から共有 kubeconfig を削除する
3. When すべての消費者の移行と共有 kubeconfig の削除が完了した, the 移行手順 shall 検証環境で手順を確認したうえで k3s のクライアント CA をローテーションし、共有 kubeconfig に含まれていたクライアント証明書を無効化する (実施時期は任意。作業中に限り SSH でのノード上のローカル admin の使用を許す。Requirement 13.2)
4. When クライアント CA のローテーションを実施した, the 移行手順 shall 旧証明書で kube-apiserver に認証できないこと、k3s とワークロードが正常に動作すること、人が証明書を再発行できることを確認する
5. If 移行の途中で新方式に不具合が見つかった, then the 移行手順 shall 共有 kubeconfig の削除前であれば従来方式で運用を継続できる状態を保つ

### Requirement 10: 進行中 PR との Infisical 認証情報の整合
**Objective:** As a インフラ担当者, I want 進行中の PR が前提にしている作成不可能な Infisical 認証情報を外したい, so that machine identity の制約内で PR をマージできる

#### Acceptance Criteria
1. The k3s ブートストラップおよび DR 復旧ワークフロー shall Infisical への認証に既存の machine identity (K3s、Viewer) の認証情報キー `INFISICAL_CLIENT_*` のみを使用する
2. The k3s ブートストラップおよび DR 復旧ワークフロー shall 作成不可能な認証情報キー (`OPS_INFISICAL_*`) と、既存 identity と同一値の重複キー (`ESO_INFISICAL_*`) を前提にしない
3. The k3s ブートストラップ (PR #288 の冪等化を含む) shall kubeconfig を Infisical へ登録する処理を含まない
4. The 本仕様の成果物 shall Infisical への書込権限を必要とする処理を含まない

### Requirement 11: 外部依存の障害時挙動
**Objective:** As a インフラ担当者, I want GitHub 障害時に何ができて何ができないかを明確にしたい, so that 障害時に誤った判断をせず手順に従って行動できる

#### Acceptance Criteria
1. While GitHub (Actions または OIDC 発行) が利用できない, the 人の認証方式 shall 発行済みで有効期限内の証明書による kubectl 操作を引き続き許可する
2. While GitHub が利用できない, the 証明書発行ワークフローおよび CI・DR ワークフロー shall 新規発行・kubectl 操作を実行できないことを許容された挙動とする
3. If kube-apiserver が GitHub Actions OIDC 発行者の公開鍵を取得できない, then the kube-apiserver shall OIDC トークンを受け入れず、クライアント証明書による認証は継続する
4. The 運用ドキュメント shall GitHub 障害時の上記挙動と、その間に取り得る対応を記載する

### Requirement 12: 操作者の識別と監査
**Objective:** As a インフラ担当者, I want kube-apiserver への操作を GitHub アカウントまたはワークフロー単位で識別したい, so that 不審な操作の調査や責任追跡ができる

#### Acceptance Criteria
1. The kube-apiserver shall 人の要求を GitHub ユーザー名と数値 ID の組に対応するユーザー名で、ワークフローの要求をワークフローファイルに対応するユーザー名で識別する
2. The 証明書発行ワークフロー shall 発行ごとに起動者・対象ユーザー名・有効期限を GitHub 上の実行記録として残す
3. The kube-apiserver shall 監査ログに要求ごとの認証されたユーザー名・操作・対象リソースを記録し、監査ログの容量をノードのディスク予算内に制限する

### Requirement 13: 制約と公開リポジトリへの配慮
**Objective:** As a インフラ担当者, I want 本移行が既存の制約を破らないことを保証したい, so that 運用コスト・循環依存・情報漏洩のリスクを増やさない

#### Acceptance Criteria
1. The 本仕様の成果物 shall 認証仲介・プロキシとなる新規アプリケーションをクラスタ内外に導入しない
2. The 本仕様の成果物 shall kubectl アクセスに SSH 経由の経路を使わず、tailnet 経由の kube-apiserver への直接接続のみを使用する。例外として、client CA の forced rotation の作業中 (k3s の再起動を伴う手順の開始から、新 CA での接続と人の再発行の確認まで) に限り、kubectl・復旧の代替手段として SSH でのノード上のローカル admin の使用を許す
3. The 本仕様の成果物 shall Zitadel を kube-apiserver の認証に使用しない
4. The 本仕様の成果物 shall Infisical machine identity を追加せず、既存 identity に書込権限を付与しない
5. The 本仕様の成果物 shall 文書・コミット・公開ログに本番の件数・シークレット値・非公開の内部 ID を含めない (GitHub API で誰でも取得できる組織・リポジトリの数値 ID は公開情報として扱ってよい)

### Requirement 14: ドキュメント同期
**Objective:** As a 引き継ぎ後の運営チーム, I want 新しい認証方式と運用手順がドキュメントに反映されていてほしい, so that 共有 kubeconfig を前提とした古い手順に従って混乱しない

#### Acceptance Criteria
1. The 運用ドキュメント (CLAUDE.md、steering、README、DR runbook、Zitadel runbook) shall 共有 kubeconfig を前提とした記述を含まず、新しい kubectl 利用手順 (証明書発行・コンテキスト作成) を記載する
2. The steering の技術ドキュメント shall Infisical で管理するシークレット一覧から廃止したキーを除き、新規キーがあれば追記する (値は含めない)
3. The steering の DR ドキュメント shall DR 復旧時の kube-apiserver 認証方式、クラスタ再作成時の人の証明書再発行の必要性、server CA 証明書の扱いを記載する
4. The 運用ドキュメント shall 権限の付与・剥奪手順 (RBAC binding の追加・削除) と、証明書が失効できないことに伴う注意点を記載する

## 決定事項
設計フェーズで次のとおり決定した (詳細は design.md の「決定済みの事項」)。
1. 人向けクライアント証明書の有効期限は 7 日 (Requirement 3.7)
2. server CA 証明書は kube-apiserver のノードから tailnet 経由で取得する (Requirement 7.1、7.5)
3. intrusion-response は他の CI と同じタグ付き方式で tailnet に参加する (Requirement 8.2)
4. k3s 監査ログを有効化する (Requirement 12.3)
5. 共有 kubeconfig 削除後に k3s のクライアント CA をローテーションする (Requirement 9.3、9.4)。実施時期は任意で、作業中に限り SSH を使ってよい (Requirement 13.2 の例外)
6. DR 復旧はクラスタ管理権限 (cluster-admin 相当) + 承認必須の Environment、k3s アップグレードには kube-apiserver の権限を与えない (Requirement 1.6、2.6)
7. 証明書発行ワークフローには起動者以外の承認を課さない (Requirement 1.6、3.1)
8. 人のユーザー名は GitHub ユーザー名と数値 ID の組とする (Requirement 3.3、5.1、5.6、12.1)
