# idp-migration-zitadel PoC総括

spec `idp-migration-zitadel` のtask 1〜8(PoCスコープ)完了時点の総括。task 9(バックアップ移行による本番カットオーバー)は本PoCの結果を踏まえて別途承認・着手を判断するものであり、本書の対象外。

## Requirements充足状況

| Requirement | 内容 | 状態 | 備考 |
|---|---|---|---|
| 1. IdP基盤のメモリ使用量削減 | CNPGベース実測でauthentik比の削減を確認 | 充足 | task1.3。CNPGベース実測値がauthentik実測(約800Mi)より明確に少ないことを確認済み |
| 2. OIDC Provider機能の継続 | CMS/Vaultwarden/Roundcubeの継続ログイン | 充足 | task2.2, 5.1〜5.3, 8.1〜8.2。3アプリともE2E成功 |
| 3. LDAP認証(メールサーバー)の継続 | Dovecot lua passdb経由のZitadel委譲 | 充足 | task3.1〜3.5, 8.3。IMAP/POP3・Roundcube双方確認済み |
| 4. Vaultwardenグループ権限同期の継続 | Actions v2 webhook + 常駐Deployment化 | 充足 | task4.1〜4.4, 8.4。安定性検証済み(下記参照)、反映遅延実測済み |
| 5. Discord連携のスコープ縮小 | 単純OAuth2ログインのみ、動的判定は非実装 | 部分充足 | task2.4はTerraform定義・認可コードフロー到達を確認したのみで、Discord実アカウントでの実接続確認は未実施(下記参照) |
| 6. 既存ユーザー・グループデータの移行 | 招待ベース移行 | 充足 | task6.1, 8.5。2名の独立したユーザーでE2E確認済み(佐藤太郎・鈴木花子) |
| 7. バックアップ移行によるカットオーバーとロールバック | 本番カットオーバー手順 | **未実施** | task9はPoCスコープ外。本番Zitadelは未デプロイ、export/import・切り戻し実地検証とも未着手 |
| 8. RBAC設計方針(フラットロール) | project role・クレーム配布 | 充足 | task2.1, 8.6。id_token/userinfo双方でクレーム反映を実機確認済み |
| 9. セキュリティ検証(モンキーテスト) | ブルートフォース・replay・PKCE・招待コード等 | 部分充足(1件不合格) | task7.1〜7.4。7.1(ブルートフォース対策)が不合格、詳細は下記「重大脆弱性」参照。7.2は合格、7.3は再利用防止のみ合格・期限切れ未検証 |
| 10. 機能検証(正常系動作確認) | 全機能の実機正常系確認 | 充足(10.8除く) | task8.1〜8.6完了。10.8(ロールバック実地検証)はtask9.6でありPoCスコープ外につき未実施 |
| 11. Terraformプロバイダー認証のブートストラップ | Ansible経由の初回PAT発行 | 充足 | task1.2。冪等な再実行動作を確認済み |

## 本番カットオーバー前の必須対応事項

### 1. lockout policyが無効(重大、対応必須)

task7.1のセキュリティ検証で、誤パスワード15〜18回の連続試行後も直後の正しいパスワードでのログインが即座に成功し、レート制限・アカウント一時ロックアウトが一切発火しないことを確認した。`GET /admin/v1/policies/lockout`は`maxPasswordAttempts`未設定のデフォルトポリシーを返しており、ブルートフォース対策が無効な状態のまま運用されている。**本番カットオーバー(task9)着手前にlockout policyの明示的な有効化を必須の前提条件とする。** 詳細は`docs/zitadel-security-poc-tests-results.md`参照。

あわせて、存在しないユーザー名(404/code5)と実在ユーザーの誤パスワード(400/code3)はSession API生レスポンスのレベルでは区別可能(ステータス・エラーコード・メッセージ・応答時間すべてで区別できる)であり、ユーザー列挙耐性も不合格。Dovecot Lua Auth Bridge側は両者を`PASSDB_RESULT_PASSWORD_MISMATCH`へ正規化しステータス面を緩和しているが、タイミングサイドチャネルは未対策のまま残る。OIDCログイン経路(RPアプリ)側の対策は別途検討が必要。

### 2. 招待コードの有効期限検証は未実施

task7.3で、`CreateInviteCode`の`expiration`フィールド指定はサイレントに無視され、デフォルト約72時間固定であることが判明した(upstream issue zitadel/zitadel#10474と整合)。72時間の実待機が非現実的なため、期限切れによる拒否そのものは確認できていない。再利用防止(2回目使用の拒否)は合格。

## task4.4: Actions v2安定性判断

k3d検証環境でuser.grantイベント条件のExecutionを繰り返し発生させ、既知のリグレッション事例(zitadel/zitadel#12225)を踏まえた安定性検証を実施した結果、**「安定」と判断し、vaultwarden-rbac-syncのイベント駆動同期(task4.1, 4.2)は本specのスコープに残置している。** 2秒間隔10回・無遅延連続10回のいずれも10/10発火、API呼び出し自体の失敗も0件だった。ただし#12225は「Group条件でなく全イベント条件Execution時のregression」を報告しており、本検証は「user.grant」グループ限定条件かつ10〜20回程度の短時間試行に留まる点は限界として記録されている。8.4で実施した追加測定(反映まで中央値約0.57秒)でも安定した動作を確認しており、この判断を覆す結果は得られていない。

## task2.4: Discord連携の確認範囲

Discordソーシャルログイン用OAuth2 IdP連携はTerraformで定義し、Discord経由の認可コードフローがOIDCトークン発行まで到達することを確認済みだが、**これは手動確認手順としての到達確認に留まり、実際のDiscordアカウントを用いた実接続(実際のDiscord OAuth同意画面を経由したログイン)は未確認**である。本番カットオーバー前に実Discordアカウントでの動作確認を追加で行うことを推奨する。

## k3d環境に残っているリソース(レビュー用)

**kubeconfig**: `/tmp/claude-1000/-home-musashi-Documents-develop-aramakisai-infra/0a8401d6-603b-4639-b713-e8787c600cb3/scratchpad/zitadel-poc/k3d-kubeconfig.yaml`
**クラスタ名**: `zitadel-poc`(削除禁止、レビュー後にユーザー判断で削除)

| Namespace | 主要Pod/Deployment | 用途 |
|---|---|---|
| `zitadel` | `zitadel-0`(StatefulSet、api+login)、`zitadel-db-1`(CNPG)、`curltest`(検証用curlpod、都度作り直し可) | Zitadel本体 |
| `poc-apps` | `vaultwarden` + `vw-postgres`、`roundcube` + `rc-dovecot`、`toolbox`(netshoot、in-cluster検証用) | task5で実デプロイしたRPアプリ |
| `mailtest` | `dovecot-zitadel-test` | task3のDovecot Lua Auth Bridge単体検証用Pod |
| `cnpg-system` | CloudNativePG Operator | CNPG基盤 |

**アクセス方法**: Zitadel/Vaultwarden/RoundcubeともExternalDomain不一致のためport-forward+Host偽装は使えず、クラスタ内DNS(`*.svc.cluster.local`)経由のみ疎通する。`poc-apps`の`toolbox`Pod(netshoot、python3同梱)へ検証スクリプトを`kubectl cp`し、`kubectl exec`で実行するのが最も簡便。`zitadel`の`curltest`Podはcurlのみでpython3非同梱、かつ`sleep`コマンドで起動しているため放置すると`Completed`状態になり再作成が必要(このセッションでも2回再作成した)。

**認証情報**: `admin-pat.txt`(Zitadel管理用PAT)、`zitadel-poc/login-client.pat`(OIDC CreateCallback用)、`creds.json`(CMS/Vaultwarden/RoundcubeのOIDC client_id/secret)、`zitadel-poc/tf-validation/`(Actions v2 Target/Execution・テストユーザー等を管理するミニTerraformプロジェクト、`zitadel_test_user.tf`のtestuser1は現在project role `planning`を付与済み、password `TestPassw0rd!`)がいずれもscratchpad配下に残っている。

**8.4で作成し削除済みのリソース**: 一時namespace`poc-webhook`(webhook受信検証用Deployment/Service)、一時namespace`prod`(vaultwarden-rbac-syncのSyncLockManagerがLease/ConfigMap操作先として`prod`をハードコードしているため作成した検証専用namespace)、Vaultwarden DB上のテスト用Organization/Collection/サービスアカウント一式。検証後にすべて削除し、Zitadel Action Target endpointとtestuser1のロール付与も検証前の状態へ復元済み。

## 本番移行(task9)着手前の追加検討事項

1. **lockout policy有効化**(上記「必須対応事項」参照、最優先)
2. Discord実アカウントでの実接続確認
3. 招待コード期限切れ動作の確認方法の検討(72時間待機が非現実的なため、代替検証方法が必要)
4. OIDCログイン経路でのユーザー列挙耐性・タイミングサイドチャネル対策の要否判断
5. task9.1の`excludedOrgIds`等、export対象からのk3d検証用データ除外フィルタの具体的な確定
6. 本番Zitadelのメモリ実測(task1.3はk3d環境での実測であり、prod-node-1実機での再実測が望ましい)
7. vaultwarden-rbac-syncの`replicas: 0`凍結解除タイミングと、`K8S_NAMESPACE`ハードコード("prod")が本番namespace構成と一致していることの再確認
8. Zitadel login v2 UIとAPIを単一オリジンへ統合するIngress/Route定義の追加(下記task10.7参照。現状未定義のためログイン画面でロゴ/iconが表示されない)

## task10: 追加移行スコープ(既存authentik付随機能6件)のk3d PoC実装

task1〜8完了後の追加検討で判明した、旧spec初版ではスコープ外だった`terraform/authentik_*.tf`6ファイル相当(enrollment/recovery・出展団体アカウント・招待用SA権限・メーリングリスト・Discordアクセス制御・ブランディング)のPoC実装。task9(本番カットオーバー)とは独立して着手可能。

| task | 内容 | 結果 |
|---|---|---|
| 10.1 | enrollment/recovery整理 | 充足。学籍番号項目付き招待は既に`scripts/zitadel-invite-migration.py`の招待コードフローへ一本化済み(学籍番号は扱っていない)であることを確認。recovery相当はZitadel標準セルフサービスリカバリーをそのまま使う設計(カスタムflow未構築)を確認。新規k3d検証はtask6.1のE2E結果を根拠に流用 |
| 10.2 | 出展団体アカウント一括作成・招待運用 | 充足。`terraform/zitadel_student_exhibitor.tf`新規作成、CSV(`terraform/data/zitadel_student_exhibitors.csv`)駆動の`for_each`でユーザー一括生成。招待コード発行はprovider未対応のため`null_resource`+`local-exec`で補完。k3d実機でCSV4件全て作成成功、1件でログインまで確認 |
| 10.3 | 招待発行用SAをORG_USER_MANAGERへ移行 | 充足。`terraform/zitadel_recovery_sa.tf`新規作成。旧`authentik_student_exhibitor_recovery_sa.tf`の独自rbac_role組み合わせをZitadel組み込みロール1つへ置換。k3d実機でuser作成/閲覧/更新/パスワードリセット/招待コード発行の5操作全て成功、instance-wide API拒否(403)によりIAM_OWNER相当権限が不要なことも確認 |
| 10.4 | メーリングリストアドレスのDovecot側完結 | 充足。8件のMLアドレスはZitadel human userとして作成しない設計であることを確認。Dovecot側に`auth-ml-userdb.conf.ext`(admin@の5エイリアス用)を追加。k3d実機で13件全ての配送成功を確認。副産物として静的userdbの`allow_all_users=yes`欠落によるLMTP配送バグを発見・修正 |
| 10.5 | Discord連携アクセス制御の廃止 | 充足。`authentik_policies.tf`の動的アクセス制御(`require_discord_link`等)を移行対象から除外。`zitadel_idp.tf`のDiscord IdPが単純ログイン手段のみでアクセス制御を伴わないことを確認 |
| 10.6 | ブランド素材のリポジトリ配置 | 充足。`gitops/manifests/prod/zitadel/branding/`へロゴ2種・favicon配置、MD5照合で原本無改変を確認。入手元パスは非記載 |
| 10.7 | Zitadel Label Policyでのブランディング設定 | colors/logo/icon/watermark/theme: 充足。font(Requirement 17.3): スコープ外。目視確認: 一部制約あり(下記) |

### task10.7: ブランディング実機検証の詳細

このセッション開始時点で、前回セッションまでに構築していたk3d `zitadel-poc`クラスタがホスト再起動により消失していた(docker全コンテナ停止)ことを検出した。task10.7の依存はtask1.2のみ(task2〜10.6は不要)のため、Zitadel core(namespace・Service・CNPG DB・StatefulSet・Ansibleブートストラップ、task1.1/1.2相当)のみを最小再構築して検証した。RPアプリ(CMS/Vaultwarden/Roundcube等)・Dovecot・招待移行データ等、他task由来のリソースはこのセッションでは再構築していない。

**colors/logo/icon/watermark/theme(充足)**: `terraform/zitadel_branding.tf`の`zitadel_label_policy`をk3d実機へ適用(メインの`terraform/`はTFCバックエンドでこのdev環境からapply不可のため、task10.2/10.3と同じく`/tmp`配下の一時ミニTerraformプロジェクトで検証)。primary/background/warn/font color(light/dark)・テーマモードauto・ウォーターマーク非表示・ログイン名フル表示は`GET /management/v1/policies/label`で実値一致を確認。logo/icon(favicon流用)はアップロード後のMD5がリポジトリ内原本と完全一致(無変形)することを確認。

**font(Requirement 17.3、スコープ外)**: 実機applyが`failed to upload font: [{0 asset request returned 400 Bad Request: open /tmp/multipart-XXX: no such file or directory []}]`という原因の分かりにくいエラーで確定的に失敗した。切り分けの結果、Zitadelのフォントアップロードは`management/v1`配下ではなく別系統の`POST /assets/v1/org/policy/label/font`(ソース`internal/api/assets/`確認)で実装されており、512KB のサーバー側上限を持つことが根本原因と判明。LINE Seed JP Regular ttf原本(約3.67MB、CJKフルカバー)は全く収まらず、ASCII+かな+全角記号(591字)まで絞れば207KBに収まりアップロード・反映も実機確認できたが、その場合は本文中の漢字がカスタムフォント非適用(システムフォントへフォールバック)になる。この制約をユーザーへ報告した結果、フォント適用自体を見送る判断となり、`font_path`/`font_hash`関連コードを`zitadel_branding.tf`から削除、k3d環境側も`RemoveCustomLabelPolicyFont`で明示的に除去した。

**目視確認(一部制約あり)**: 検証用の一時OIDCクライアントでauthRequestを発行し、claude-in-chromeで実ブラウザ表示・ネットワークログを確認した。CSS内の色コード反映と"Powered by ZITADEL"非表示は確認できたが、**ロゴ/icon画像はブラウザ上で404となり表示されなかった**。原因はbranding設定の誤りではなく、login v2 UI(StatefulSetのloginコンテナ、port 3000)がアセットを自身のオリジンからの相対パス(`/assets/v1/...`)で取得しようとするため、Zitadel API(アセット実体、port 8080)と別オリジンのままでは解決できないという構造的制約。アセット自体はAPI側(port 8080)へ直接アクセスすればMD5一致で正しく取得できることを確認済みであり、アップロード・保存自体には問題がない。**本番はcloudflared等のリバースプロキシで`/assets`パスをAPI側Serviceへ、それ以外をlogin側Serviceへ振り分ける単一オリジン構成が必須だが、現状`gitops/manifests/prod/zitadel/`にはその振り分けを行うIngress/Route定義が存在しない。** 上記「本番移行(task9)着手前の追加検討事項」に項目8として追記した。

**このセッションでのk3d環境の後片付け**: 検証用に作成した一時OIDCクライアント(`zitadel_project`/`zitadel_application_oidc`)は`terraform destroy`で削除済み。port-forwardプロセスも停止済み。`zitadel_label_policy`(colors/logo/icon/watermark/theme、font無し)はk3d環境に残置している。

**旧「k3d環境に残っているリソース」セクション(41行目以降)について**: 上記の通り前回セッションのk3dクラスタ自体が本セッション開始時点で消失していたため、同セクションに記載のnamespace構成(`poc-apps`/`mailtest`等)・認証情報ファイルは全て失われており、現在のk3d `zitadel-poc`クラスタには存在しない(現在はZitadel core一式のみ)。同セクションは歴史的記録として残すが、最新状態としては参照しないこと。
