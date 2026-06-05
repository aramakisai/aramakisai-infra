# Gap Analysis: stalwart-auth-unified

## 0. コミット履歴から読み取れる本質

`git log --oneline -- gitops/manifests/prod/stalwart/settings-configmap.yaml` で確認した変遷：

- `Authentication.directoryId` を `authentik-oidc` ↔ `authentik-ldap` で複数回往復
- `issuerUrl` を `aramakisai-mail` / `stalwart-webui` / グローバル issuer と多パターン試行
- PKCE 要否確認、Public/Confidential クライアントの切り替え
- per-listener directoryId の試行

**結論:** Authentik を直接 OIDC ソースとして OAUTHBEARER を動かすあらゆる組み合わせを試し尽くした結果が現在の状態。
LDAP パスワード認証は `ef6c5bd` で URL を正しく修正して以来、安定して動作している。
**残課題は OAUTHBEARER の一点のみ。** Dex を介在させることで Stalwart の OIDC トークン検証ライブラリとの互換性問題を回避する。

---

## 1. 現状調査

### 調査対象ファイル

| ファイル | 役割 |
|----------|------|
| `gitops/manifests/prod/stalwart/statefulset.yaml` | Stalwart Pod 定義・RECOVERY_ADMIN 環境変数 |
| `gitops/manifests/prod/stalwart/settings-configmap.yaml` | settings.ndjson / settings-update.ndjson（認証設定 SSOT） |
| `gitops/manifests/prod/stalwart/settings-apply-job.yaml` | ArgoCD PostSync Job（stalwart-cli による設定適用） |
| `gitops/manifests/prod/stalwart/external-secret.yaml` | stalwart-secrets の ESO 定義 |
| `gitops/manifests/prod/authentik/ldap-outpost.yaml` | Authentik LDAP Outpost Deployment + Service + ExternalSecret |
| `gitops/manifests/prod/roundcube/config-configmap.yaml` | Roundcube の OAuth2/OAUTHBEARER 設定 |
| `gitops/manifests/prod/roundcube/external-secret.yaml` | roundcube-secrets の ESO 定義 |

### 実装済みコンポーネント

| コンポーネント | 状態 | 備考 |
|--------------|------|------|
| Stalwart StatefulSet | ✅ 実装済み | `STALWART_RECOVERY_ADMIN` 常設・Stakater Reloader 設定済み |
| ArgoCD PostSync Job | ✅ 実装済み | admin 認証・settings-update.ndjson 適用・Domain.directoryId 動的設定 |
| settings-update.ndjson | ✅ 実装済み | authentik-ldap + authentik-oidc の両ディレクトリ定義 |
| Roundcube OAUTHBEARER 設定 | ✅ 実装済み | `imap/smtp_auth_type=OAUTHBEARER`・`oauth_client_id=aramakisai-mail` |
| Authentik LDAP Outpost Deployment | ✅ 実装済み | Service名 `authentik-ldap-outpost`・ポート 389 |
| ExternalSecret (stalwart-secrets) | ✅ 実装済み | STALWART_ADMIN_SECRET, STALWART_JOB_TOKEN 等 5 キー |
| ExternalSecret (roundcube-secrets) | ✅ 実装済み | MAIL_OAUTH2_CLIENT_SECRET, ROUNDCUBE_DES_KEY |
| VolSync ReplicationSource/Destination | ✅ 実装済み | PVC バックアップ基盤 |

---

## 2. 要件との照合・ギャップ一覧

### Requirement 1: Authentik 単一ソース認証基盤

| 受け入れ基準 | 状態 | 詳細 |
|------------|------|------|
| 両ディレクトリ (LDAP + OIDC) で起動 | ✅ コード済み | settings-update.ndjson に定義 |
| パスワード変更の即時反映 | ⚠️ 要調査 | LDAP ディレクトリ接続が実際に動作しているか未確認 |
| アカウント無効化の即時反映 | ⚠️ 要調査 | 同上 |
| bindSecret を env var から取得 | ✅ コード済み | `variableName: STALWART_JOB_TOKEN` |
| LDAP 一時障害時の動作 | ✅ Stalwart 組み込み機能 | 追加実装不要 |

### Requirement 2: LDAP パスワード認証

| 受け入れ基準 | 状態 | 詳細 |
|------------|------|------|
| IMAPS PLAIN/LOGIN 認証 | ⚠️ **ギャップあり** | LDAP URL が実際の Service 名と不一致（後述） |
| SMTP PLAIN/LOGIN 認証 | ⚠️ **ギャップあり** | 同上 |
| Domain.directoryId = authentik-ldap | ✅ コード済み | PostSync Job が動的に設定 |
| 認証失敗時のエラー返却 | ✅ Stalwart 組み込み機能 | 追加実装不要 |
| TLS 必須 | ✅ NetworkListener 設定済み | IMAPS:993, SMTPS:465 は TLS |

### Requirement 3: OAUTHBEARER 認証

| 受け入れ基準 | 状態 | 詳細 |
|------------|------|------|
| Roundcube IMAP OAUTHBEARER | ✅ コード済み | imap_auth_type=OAUTHBEARER |
| Roundcube SMTP OAUTHBEARER | ✅ コード済み | smtp_auth_type=OAUTHBEARER |
| authentik-oidc ディレクトリ設定 | ✅ コード済み | issuerUrl/requireAudience=aramakisai-mail |
| 期限切れトークンの適切なエラー | ✅ Stalwart 組み込み機能 | 追加実装不要 |
| Authentication.directoryId = oidc | ✅ コード済み | settings-update.ndjson で設定 |

### Requirement 4: 認証方式の共存

| 受け入れ基準 | 状態 | 詳細 |
|------------|------|------|
| Authentication(OIDC) + Domain(LDAP) 共存 | ✅ コード済み | PostSync Job が両方設定 |
| OAUTHBEARER は OIDC で検証 | ✅ コード済み | Authentication.directoryId=OIDC |
| PLAIN/LOGIN は LDAP で検証 | ⚠️ **ギャップあり** | LDAP URL 不一致により LDAP 接続が失敗する可能性 |

### Requirement 5: ArgoCD PostSync Job

| 受け入れ基準 | 状態 | 詳細 |
|------------|------|------|
| ArgoCD sync 時に自動適用 | ✅ 実装済み | PostSync Hook 設定済み |
| Recovery Admin 認証で動作 | ✅ 実装済み | `--user admin --password $STALWART_ADMIN_SECRET` |
| Domain.directoryId 動的設定 | ✅ 実装済み | stalwart-cli query Directory で LDAP ID 取得 |
| 最大 120 秒リトライ | ✅ 実装済み | 5 秒 × 24 回 |
| 失敗時 Failed 状態維持 | ✅ 実装済み | backoffLimit: 2 |
| 設定後 Pod 再起動 | ✅ 実装済み | k8s API で stalwart-0 削除 |
| API キー方式を使用しない | ✅ 実装済み | STALWART_API_KEY は external-secret から削除済み |

### Requirement 6: VolSync バックアップ整合性

| 受け入れ基準 | 状態 | 詳細 |
|------------|------|------|
| バックアップ信頼タイミングの明示 | ✅ 文書化済み | dr.md に記載 |
| Directory 再作成後のアカウント ID 一致 | ✅ 実装済み | Domain は destroy しない運用 |
| Domain destroy 禁止の明示 | ✅ 実装済み | settings-configmap.yaml コメントに記載 |
| VolSync との並行書き込み | ✅ 実装済み | ReplicationSource 設定済み |

### Requirement 7: 管理者アクセス回復性

| 受け入れ基準 | 状態 | 詳細 |
|------------|------|------|
| STALWART_RECOVERY_ADMIN 常設 | ✅ 実装済み | statefulset.yaml env に定義 |
| ESO + Reloader 連携 | ✅ 実装済み | stalwart-secrets 変更で自動再起動 |
| パスワードローテーション後の認証成功 | ✅ 実装済み | Reloader が env を更新 |
| 失敗時の診断ログ | ✅ Stalwart 組み込み機能 | 追加実装不要 |
| API キー方式廃止 | ✅ 実装済み | external-secret.yaml から STALWART_API_KEY 削除済み |

---

## 3. 重要なギャップ詳細

### ギャップ 1（誤判定・訂正）: LDAP Service URL について

**当初の判定:** `ak-outpost-stalwart-ldap-outpost` と Service 名 `authentik-ldap-outpost` が不一致として Critical と評価した。

**訂正:** コミット `ef6c5bd fix(stalwart): use correct Authentik LDAP outpost URL (ak-outpost-stalwart-ldap-outpost)` にて**意図的にこの URL に修正された**。Authentik embedded outpost が実際に `ak-outpost-stalwart-ldap-outpost` という名前の Service を作成しており、URL は正しい。`authentik-ldap-outpost` は別途管理されている standalone Deployment であり、両者は別物。

**実際のステータス:** ✅ 問題なし。LDAP パスワード認証は想定通り動作している。

---

### ギャップ 2（Low / 確認事項）: LDAP bindDn と bindSecret の整合性

**問題:**
settings-configmap の bindDn:
```
cn=stalwart-service,ou=users,dc=ldap,dc=goauthentik,dc=io
```
しかし `external-secret.yaml` コメントと `ldap-outpost.yaml` コメントには `cn=ldapservice` の記述もある。

Authentik LDAP Outpost の service account は通常 `cn=ldapservice,dc=ldap,dc=goauthentik,dc=io` に作成される。
`cn=stalwart-service` が Authentik に実際に存在するユーザーかどうかを確認する必要がある。

また `external-secret.yaml` には以下の 2 つのトークンが存在する:
- `STALWART_JOB_TOKEN` → settings-configmap の bindSecret として使用中
- `AUTHENTIK_LDAP_OUTPOST_TOKEN` → コメントでは「cn=ldapservice の bindSecret」とあるが settings では未使用

Infisical 上でこれらが同一値か異なる値かを確認する必要がある。

**確認事項:**
1. Authentik Admin UI で `cn=stalwart-service` ユーザーが存在するか
2. `STALWART_JOB_TOKEN` と `AUTHENTIK_LDAP_OUTPOST_TOKEN` が同一トークンか

---

### ギャップ 3（Research Needed）: Authentik 側の `aramakisai-mail` OIDC Provider

**問題:**
Stalwart の authentik-oidc ディレクトリは以下を期待している:
- issuerUrl: `https://idp.aramakisai.com/application/o/aramakisai-mail/`
- requireAudience: `aramakisai-mail`

Roundcube の oauth_client_id も `aramakisai-mail` で整合しているが、
Authentik WebUI で実際に slug=`aramakisai-mail` の OAuth2 Provider + Application が作成されているかを
実際のクラスター接続なしには確認できない。

`ldap-outpost.yaml` のコメントに Authentik WebUI での設定手順が記載されているが、
実施済みかどうかは Authentik の状態に依存する。

**関連する文書の不整合:**
- `CLAUDE.md` の「Roundcube Webmail > Stalwart の OIDC 設定」は古いまま（`roundcube` → `aramakisai-mail` への更新が反映されていない）
- `config-configmap.yaml` のコメント `slug=roundcube, client_id=roundcube` も古い

---

## 4. 実装アプローチ

### アプローチ変更の背景

Authentik の OIDC トークンを Stalwart が直接検証する構成では OAUTHBEARER が動作しないことが確認済み。
根本原因の調査は完了しており、**Dex を OIDC ブローカーとして介在させる**アプローチで検証する。

### Option A: Dex 新規導入（採用）

**新規追加ファイル:**
```
gitops/apps/prod/dex.yaml                    ← ArgoCD Application
gitops/manifests/prod/dex/
  namespace.yaml
  deployment.yaml (or Helm values)
  service.yaml
  configmap.yaml                              ← Dex 設定（connector: Authentik OIDC）
  external-secret.yaml                        ← Dex client_secret を ESO 経由で注入
```

**既存ファイルの変更:**
```
gitops/manifests/prod/stalwart/settings-configmap.yaml
  - authentik-oidc ディレクトリ → dex-oidc ディレクトリに置き換え
  - issuerUrl を Dex のエンドポイントに変更

gitops/manifests/prod/roundcube/config-configmap.yaml
  - oauth_auth_uri / oauth_token_uri / oauth_identity_uri を Dex に向け直す

gitops/manifests/prod/stalwart/settings-apply-job.yaml
  - settings-update.ndjson 内の Directory destroy+create 後に dex-oidc を使う
```

**Trade-offs:**
- ✅ Authentik の OIDC 実装の制約を回避できる
- ✅ Dex は標準 OIDC に準拠しており Stalwart との互換性が高い
- ✅ 既存の GitOps パターン（ArgoCD App + ESO）をそのまま踏襲
- ❌ 新しいコンポーネント（Dex）の管理が増える
- ❌ トークン連鎖（Roundcube → Dex → Authentik）により障害点が増える

### Option B: Authentik 直接接続の修正継続（不採用）

Authentik の OIDC 設定を変更して Stalwart との互換性を高める方向。
動作確認が取れていないため、現時点では採用しない。

---

## 5. 実装複雑度とリスク（更新）

| 項目 | 工数 | リスク | 根拠 |
|------|------|--------|------|
| Dex デプロイ（新規マニフェスト） | M | Medium | Dex 設定（connector / client 設定）は新規。Helm chart があるため複雑度は低め |
| Roundcube OAuth2 endpoint 切り替え | S | Low | config-configmap の URL 変更のみ |
| Stalwart settings-configmap 更新 | S | Low | authentik-oidc → dex-oidc への文字列変更 |
| Dex シークレット管理（ESO/Infisical） | S | Low | 既存パターン（ExternalSecret）をそのまま使用 |
| LDAP URL 修正（ギャップ 1） | S | Low | 文字列置換、LDAP パスワード認証の修正 |
| **合計** | **M** | **Medium** | 新コンポーネント導入だが既存パターン踏襲で管理可能 |

---

## 6. 設計フェーズへの推奨事項

### 設計で確定すべき決定事項

1. **Dex のデプロイ方式**: 公式 Helm chart (`charts.dex.dev/dex`) vs 生マニフェスト
2. **Dex の公開方式**: クラスター内専用（ClusterIP のみ）vs Cloudflare Tunnel 経由で外部公開（Roundcube が Tunnel 外から Dex にアクセスする場合）
3. **Dex の issuer URL**: Roundcube と Stalwart が同じ URL で到達できる必要がある（クラスター内外で統一）
4. **Authentik OIDC connector の設定**: Dex が Authentik に接続するための client_id / client_secret（Authentik に新規 OAuth2 Provider 作成が必要か）
5. **LDAP URL 修正**: `ak-outpost-stalwart-ldap-outpost` → `authentik-ldap-outpost` への修正を同時に含めるか
