# Research & Design Decisions

## Summary
- **Feature**: `authentik-ldap-bind-failure`
- **Discovery Scope**: Extension (既存システムの障害調査・修正)
- **Key Findings**:
  - Authentik のデフォルト認証フローのパスワードステージは4つのバックエンド (InbuiltBackend, KerberosBackend, LDAPBackend, TokenBackend) を持つが、現在の LDAP bind 専用フローは `InbuiltBackend` のみ。Web ログインはデフォルトフローを使うため問題ないが、LDAP bind フロー側の `InbuiltBackend` のみが LDAP Outpost 経由で失敗する根本原因は未確定
  - `bind_mode = "cached"` + `search_mode = "cached"` の組み合わせでは bind は成功するが search が Operations error になることが既知 (`authentik_ldap.tf` のコメントに記録済み)
  - `exceeded stage recursion depth` は複数 issue (#6523, #14210, #18421) で報告されている LDAP bind 失敗の典型的な症状。flow/stage 設定の不整合や identification stage のループが原因

## Research Log

### 否定済み仮説の再確認 (9件)
- **Context**: requirements.md に記録済みの9件の仮説。再試行による時間の浪費を防ぐため再確認する
- **Sources**: 本プロジェクトの `terraform/authentik_ldap.tf` コメント、commit `4fc7826`
- **Findings**:
  - Infisical⇄k8s Secret パスワードドリフト: 根治後も再現 → 除外
  - パスワード文字種: base64特殊文字 vs 英数字のみ → 効果なし
  - brute-force/reputation: Events ログで2回のみ、ロックアウト表示なし → 除外
  - `mfa_support`: true→false → 効果なし
  - Authenticator Validation ステージ: LDAP bind 専用最小 flow 切り替え → 効果なし
  - バージョン不一致: 2026.5.2→2026.5.3 統一 → 再現
  - flow `authentication` 設定: none→require_outpost で別エラー発生 → 除外
  - 全リソース再作成: GitHub issue #14210 の解決例に倣う → 再現
  - App Password (TokenBackend): 同様に失敗 → 除外
- **Implications**: 残る仮説は「LDAP Outpost → Authentik Server 間のフロー実行パス固有の問題」に限定される

### goauthentik 既知 issue との比較
- **Context**: 同一症状 (Web 成功・LDAP 失敗・パスワード正確) の報告を検索
- **Sources Consulted**:
  - [Issue #14210](https://github.com/goauthentik/authentik/issues/14210) — LDAP always returns error: Invalid credentials (2025-04)
  - [Issue #18421](https://github.com/goauthentik/authentik/issues/18421) — LDAP not working after upgrade (2025-11)
  - [Issue #6523](https://github.com/goauthentik/authentik/issues/6523) — LDAP Outpost fails with "exceeded stage recursion depth" (2023-08)
  - [Issue #10571](https://github.com/goauthentik/authentik/issues/10571) — LDAP outpost doesn't work if user has TOTP (2024-07)
  - [Issue #19970](https://github.com/goauthentik/authentik/issues/19970) — self-deployed LDAP outpost not working (2026-02)
  - [Issue #2743](https://github.com/goauthentik/authentik/issues/2743) — LDAP not working, Invalid credentials (2022-04)
  - [Issue #11324](https://github.com/goauthentik/authentik/issues/11324) — LDAP Invalid credentials after update (2024-09)
  - [r/Authentik LDAP recursion depth](https://www.reddit.com/r/Authentik/comments/1qn36zd/ldap_recursion_depth_issue/) (2026-01)
- **Findings**:
  - #14210: identification stage がループし `exceeded stage recursion depth` に到達。InbuiltBackend が何も返さず TokenBackend にフォールバック。flow の `authentication = require_outpost` で "Attempted remote-ip override without token" エラーも報告。解決策なし (open)
  - #18421: アップグレード後 (2025.6→2025.10) に LDAP が "Invalid Credentials"。blueprint で password stage が flow に bind されていないことが原因
  - #6523: 2023.6.1 で recursion depth エラー。closed as "legacy/wontfix"
  - #10571: TOTP 有効ユーザーで LDAP 失敗。MFA ステージが LDAP bind で通過不可
  - #19970: 2025.12.3 で LDAP イメージが server として起動しようとするバグ。2025.12.1 へのダウングレードで解決
  - #2743: cached → direct に切り替え + サービスユーザー作成で解決
- **Implications**: 本件の症状は #14210 に最も近い (Web 成功・LDAP 失敗・`authentication = none`・`require_outpost` で別エラー)。ただし本件は `exceeded stage recursion depth` ログは未確認。DEBUG ログで確認が必要

### Authentik デフォルト認証フローのパスワードバックエンド構成
- **Context**: デフォルトフローのパスワードステージが持つバックエンド一覧と、LDAP bind 専用フローとの差異
- **Sources Consulted**:
  - [default-authentication-flow.yaml (GitHub)](https://github.com/goauthentik/authentik/blob/main/blueprints/default/flow-default-authentication-flow.yaml)
  - [password/models.py (GitHub)](https://github.com/goauthentik/authentik/blob/master/authentik/stages/password/models.py)
  - [GoAuthentik from A to Y (Blog)](https://a-cup-of.coffee/blog/goauthentik/)
- **Findings**:
  - デフォルト認証フローのパスワードステージは4バックエンド: `InbuiltBackend`, `KerberosBackend`, `LDAPBackend`, `TokenBackend`
  - 現在の LDAP bind フロー (`authentik_stage_password.ldap_bind_password`) は `InbuiltBackend` のみ
  - ブログ記事では LDAP 用でも "User database + standard password" + "User database + app passwords" + "User database + LDAP password" の3つを推奨
  - `LDAPBackend` は外部 LDAP ソースへの認証委譲用 (Authentik 内部ユーザーには不要)
  - `TokenBackend` は App Password 検証用。仮説9で TokenBackend 経由も失敗済みだが、これはバックエンドリストに含めていない状態での検証
- **Implications**: `InbuiltBackend` 単体構成自体は Authentik 内部ユーザーに対して正しい。Web ログインも同じ `InbuiltBackend` を使うため、問題の本質はバックエンド構成ではなくフロー実行パスにある可能性が高い

### LDAP Outpost の bind_mode / search_mode 挙動
- **Context**: `bind_mode = "cached"` + `search_mode = "cached"` で bind 成功・search Operations error の報告がある
- **Sources Consulted**:
  - [LDAP Provider docs](https://docs.goauthentik.io/add-secure-apps/providers/ldap/)
  - `terraform/authentik_ldap.tf` のコメント (commit `4fc7826`)
- **Findings**:
  - Direct bind: 毎回フロー実行。最も正確だがパフォーマンスコスト大
  - Cached bind: フロー実行結果をメモリキャッシュ。セッションは独立しており、資格情報変更やセッション失効は即座に反映されない
  - Direct search: 毎回 API 呼び出し。最新データだが遅い
  - Cached search: 定期的に全ユーザー/グループをメモリに取得。高速だが古いデータの可能性
  - 現在の構成: `bind_mode = "cached"`, `search_mode = "cached"`
  - `authentik_ldap.tf` のコメント: direct binder で bind 成功後の `/api/v3/core/users/me/` が 403 になり search が Operations error
- **Implications**: bind_mode を `direct` に戻した状態での検証が必須。cached モードは障害のマスキング要因になり得る

### フローの `authentication` 設定と LDAP Outpost の関係
- **Context**: `authentication = "none"` vs `"require_outpost"` の挙動差
- **Sources Consulted**:
  - `terraform/authentik_ldap.tf` のコメント
  - [Issue #14210](https://github.com/goauthentik/authentik/issues/14210)
- **Findings**:
  - `authentication = "none"`: 誰でもフロー実行可能。LDAP Outpost 経由で Invalid credentials (49)
  - `authentication = "require_authentication"`: 認証済みユーザーのみフロー実行可能。LDAP bind では使えない
  - `authentication = "require_outpost"`: Outpost トークンが必要。"Flow not applicable to current user" + "Attempted remote-ip override without token" の新エラー
  - デフォルト認証フロー (`default-authentication-flow`) も `authentication = "none"`
  - `require_outpost` は Outpost がトークンを使って API を呼ぶ必要があるが、LDAP Outpost のトークン認識に問題がある可能性
- **Implications**: `authentication = "none"` は正しい設定。問題はここではない

### デバッグアプローチの評価
- **Context**: 残された調査手段の優先順位付け
- **Sources Consulted**: requirements.md の「未検証・次の手がかり」セクション
- **Findings**:
  - DEBUG ログ: authentik-server の `log_level` を `debug` に変更 (`AUTHENTIK_LOG_LEVEL=debug`) で `PasswordStageView` の `authenticate()` 呼び出し詳細、バックエンド評価順、リクエストパラメータが可視化される
  - Django shell: `User.objects.get(username="ml-pr").check_password(<password>)` でハッシュ比較自体の成否を直接確認。フロー実行をバイパスするため、DB 層の問題かフロー層の問題かを切り分け可能
  - LDAP Outpost のログレベル: `log_level` を `debug` に変更すると Outpost 側での bind/search 処理の詳細が見える
  - Cilium NetworkPolicy: Outpost → Server 間の通信が変質していないかの確認。ただし HTTP 通信なので可能性は低い
- **Implications**: DEBUG ログ + Django shell の2つが最優先。並列実行可能

## Architecture Pattern Evaluation

本 spec は新規アーキテクチャの選択ではなく、既存システムの障害調査・修正が目的。パターン評価は不要。

## Design Decisions

### Decision: 調査アプローチ — レイヤー分離デバッグ
- **Context**: 9件の仮説を否定済み。残るは LDAP Outpost 経由のフロー実行パス固有の問題
- **Alternatives Considered**:
  1. カスタム bind フローを破棄して `default-authentication-flow` を直接使用 — Web ログインと同じパスを使うが、MFA ステージが含まれるため LDAP bind では別の失敗を引き起こす
  2. bind_mode/search_mode を全パターン (direct/direct, direct/cached, cached/direct, cached/cached) 試行 — 時間がかかるが体系的
  3. レイヤー分離デバッグ (DB 層 → フロー層 → ネットワーク層の順に切り分け) — 最も効率的
- **Selected Approach**: レイヤー分離デバッグ。まず Django shell で DB 層のパスワードハッシュ検証を確認し、次に DEBUG ログでフロー実行パスの詳細を可視化、最後に必要に応じてネットワーク層を検証
- **Rationale**: DB 層の問題でなければフロー実行パスに原因を限定できる。DEBUG ログで具体的な失敗箇所が分かる
- **Trade-offs**: DEBUG ログは一時的なパフォーマンスオーバーヘッドがあるが、調査中のみ許容
- **Follow-up**: bind_mode/search_mode の全パターン検証は、根本原因特定後に最適構成を決定する際に実施

### Decision: 修正の Terraform 管理原則
- **Context**: 修正内容が Authentik WebUI のみの暫定対応にならないよう要件 2.4 で規定
- **Alternatives Considered**:
  1. WebUI で直接修正 — 即効性はあるがコード化されず、再現性が失われる
  2. Terraform で管理 — 変更履歴・レビュー・再現性が担保される
- **Selected Approach**: 全ての修正は `terraform/authentik_ldap.tf` または `gitops/` 配下のマニフェスト変更として適用
- **Rationale**: 運営チームへの引き継ぎと Infrastructure as Code の原則維持
- **Trade-offs**: 調査中の検証的変更も Terraform で管理するため、一時的な trial-and-error のコミットが増える可能性がある
- **Follow-up**: 調査中の trial 変更は `WIP` プレフィックスのコミットで区別する

### Decision: 既存構成の維持方針
- **Context**: `authentik_flow.ldap_bind` (LDAP bind 専用最小フロー) は構成の簡素化として妥当
- **Alternatives Considered**:
  1. 専用フローを維持 — MFA 除外の明示的管理が可能
  2. `default-authentication-flow` に統合 — 管理は簡素化するが MFA ステージの問題が残る
- **Selected Approach**: LDAP bind 専用フロー (`identification + password` のみ) を維持。`authentication = "none"` も維持
- **Rationale**: Web ログインフローに影響を与えず、LDAP 固有の制約 (MFA 非対応) を明示的に管理

## Risks & Mitigations

- **根本原因が Authentik 本体のバグである可能性** — Issue #14210 が open のまま解決されていない。バグの場合、ワークアラウンド (例: `bind_mode = "cached"` + 別途 search 検証) での回避が必要
- **DEBUG ログ有効化による本番パフォーマンス影響** — 調査中のみ有効化し、調査完了後に `info` に戻す。Authentik Helm values (`log_level`) の一時的変更
- **調査中の trial-and-error による一時的なサービス停止** — DMS の LDAP 問い合わせは既に停止中のため、追加の影響は限定的。ただし Web/OIDC 認証への影響は必ず回避
- **bind_mode/search_mode の変更による副作用** — `cached` → `direct` に戻すとパフォーマンスが低下するが、正確性が優先。最適化は根本原因解決後に実施

## References

- [Issue #14210 — LDAP always returns error: Invalid credentials](https://github.com/goauthentik/authentik/issues/14210) — 最も症状が近い (Web 成功・LDAP 失敗・authentication=none)
- [Issue #18421 — LDAP not working after upgrade](https://github.com/goauthentik/authentik/issues/18421) — password stage の flow binding 忘れパターン
- [Issue #6523 — exceeded stage recursion depth](https://github.com/goauthentik/authentik/issues/6523) — recursion depth エラーの既知 issue
- [Issue #10571 — LDAP outpost doesn't work if user has TOTP](https://github.com/goauthentik/authentik/issues/10571) — MFA ステージ問題
- [Issue #19970 — self-deployed LDAP outpost not working](https://github.com/goauthentik/authentik/issues/19970) — バージョン固有バグ
- [Issue #2743 — LDAP not working, Invalid credentials](https://github.com/goauthentik/authentik/issues/2743) — cached→direct 切り替えで解決の事例
- [LDAP Provider docs](https://docs.goauthentik.io/add-secure-apps/providers/ldap/) — bind_mode / search_mode の公式ドキュメント
- [Create an LDAP provider](https://docs.goauthentik.io/add-secure-apps/providers/ldap/create-ldap-provider/) — フロー構成の公式手順
- [default-authentication-flow.yaml](https://github.com/goauthentik/authentik/blob/main/blueprints/default/flow-default-authentication-flow.yaml) — デフォルト認証フローの blueprint
- [password/models.py](https://github.com/goauthentik/authentik/blob/master/authentik/stages/password/models.py) — パスワードバックエンドのソースコード
- [GoAuthentik from A to Y](https://a-cup-of.coffee/blog/goauthentik/) — LDAP 設定の実践ガイド
