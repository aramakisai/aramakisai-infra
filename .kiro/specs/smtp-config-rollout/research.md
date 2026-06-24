# Research & Design Decisions Template

## Summary
- **Feature**: `smtp-config-rollout`
- **Discovery Scope**: Extension（既存 Authentik SMTP リレーを Vaultwarden / Directus に横展開する設定変更のみ。新規コンポーネント・新規外部サービスなし）
- **Key Findings**:
  - Vaultwarden は `SMTP_*` 環境変数群（公式 `.env.template` 準拠）に対応しているが、現在 `deployment.yaml` に一切設定されておらず、`external-secret.yaml` にも `SMTP_PASSWORD` がコメントアウトされたまま残っている。
  - Directus は `EMAIL_TRANSPORT=smtp` と `EMAIL_SMTP_*` 環境変数群に対応しているが、現在メール関連設定が一切存在しない。
  - Authentik はすでに `mail.aramakisai.com:587`（ユーザー `noreply`、STARTTLS、Infisical キー `NOREPLY_SMTP_PASSWORD`）で稼働実績があり、mailserver 側の `SPOOF_PROTECTION`（SASL認証ユーザーとMAIL FROMの一致要求）もこの `noreply` アカウント向けに既に許可済み。同じアカウント・同じFromアドレスを再利用すれば mailserver 側の追加対応は不要。

## Research Log

### Vaultwarden SMTP 環境変数の仕様確認
- **Context**: `deployment.yaml` に SMTP 関連 env が無く、対応する変数名・既定値を確認する必要があった。
- **Sources Consulted**: dani-garcia/vaultwarden 公式 `.env.template`（GitHub）
- **Findings**:
  - `SMTP_HOST`, `SMTP_PORT`（既定587）, `SMTP_SECURITY`（既定 `starttls`、他に `force_tls`/`off`）, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_FROM_NAME`, `SMTP_AUTH_MECHANISM`（既定 `Plain, Login`）
  - `SMTP_USERNAME` を指定する場合 `SMTP_PASSWORD` は必須
- **Implications**: 既定の `SMTP_SECURITY=starttls` と `SMTP_PORT=587` が Authentik と同条件のため、`SMTP_SECURITY` は明示指定不要だが、レビュー時の可読性のため明示する方針とする。

### Directus SMTP 環境変数の仕様確認
- **Context**: Directus 側のメール送信機能（パスワードリセット・ユーザー招待）に必要な変数を確認。
- **Sources Consulted**: directus.com 公式ドキュメント `/docs/configuration/email`
- **Findings**:
  - `EMAIL_TRANSPORT=smtp`, `EMAIL_FROM`, `EMAIL_SMTP_HOST`, `EMAIL_SMTP_PORT`, `EMAIL_SMTP_USER`, `EMAIL_SMTP_PASSWORD`, `EMAIL_SMTP_SECURE`（TLS有効化）, `EMAIL_SMTP_IGNORE_TLS`, `EMAIL_SMTP_POOL`
  - Directus は内部で Nodemailer を使用する構成のため、`EMAIL_SMTP_SECURE=true` は暗黙的TLS（465番ポート想定）を意味する。587番 + STARTTLS の場合は `EMAIL_SMTP_SECURE` を `false`（既定）のままにし、`EMAIL_SMTP_IGNORE_TLS` も `false`（既定）のままにすることで、サーバーが STARTTLS をアドバタイズした場合に自動アップグレードされる。
- **Implications**: `EMAIL_SMTP_SECURE=true` を誤って設定すると、587番ポートで暗黙的TLSハンドシェイクを試み接続失敗するリスクがある。Directus側は `EMAIL_SMTP_SECURE` / `EMAIL_SMTP_IGNORE_TLS` を**未設定（デフォルトのfalse）**のままにする設計とする。

### mailserver SPOOF_PROTECTION との整合性確認
- **Context**: Requirement 1.4 / 2.4（認証情報重複禁止）および誤送信防止の観点で、新規送信元が `SPOOF_PROTECTION` に阻まれないか確認。
- **Sources Consulted**: `gitops/manifests/prod/mailserver/configmap.yaml`（`ldap-senders.cf` 生成ロジック）、直近コミット `7c8978d fix(mailserver): allow noreply to send despite SPOOF_PROTECTION sender filter`
- **Findings**: `SPOOF_PROTECTION` は SASL認証ユーザー名のLDAP `mail` 属性とMAIL FROMの一致を要求する（`reject_sender_login_mismatch`）。`noreply` アカウントは既にこのチェックを通過する設定済み。認証はアカウント単位であり、接続元Podやアプリケーションには依存しない。
- **Implications**: Vaultwarden / Directus が同じ `noreply` アカウント（同一ユーザー名・パスワード・Fromアドレス）で認証する限り、mailserver側の追加変更は不要。Fromアドレスを `noreply@aramakisai.com` 以外に変えるとSPOOF_PROTECTIONに阻まれるため、各サービスのFromアドレスは固定とする。 <!-- confidential:allow -->

### Reloader アノテーション網羅状況の確認
- **Context**: シークレット値のローテーション時に Pod が自動再起動されるか確認。
- **Sources Consulted**: `grep -rn "reloader.stakater.com" gitops/`
- **Findings**: `vaultwarden`, `roundcube`, `mailserver`, `authentik/ldap-outpost` は `secret.reloader.stakater.com/reload` アノテーションを持つが、`directus/deployment.yaml` には一切付与されていない（既存のギャップ）。
- **Implications**: Directus に SMTP パスワードを追加するこの変更を機に、`directus-secrets` 用の reloader アノテーションも合わせて追加する（既存パターンへの整合）。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| 既存 Infisical キー (`NOREPLY_SMTP_PASSWORD`) を複数 ExternalSecret から参照 | Vaultwarden/Directus の ExternalSecret の `remoteRef.key` をAuthentikと同じキーに向ける | シークレット値の一元管理、ローテーション時の更新箇所が1ヶ所、`VAULTWARDEN_SMTP_PASSWORD`等の未使用キーを作らない | 3サービスが同一認証情報に依存するため、パスワード漏洩時の影響範囲がやや広がる（ただし元々同一`noreply`アカウントの権限範囲なので実質増加なし） | 既存の `DISCORD_OPS_WEBHOOK_URL` 共用パターンと同種 |
| サービスごとに新規 Infisical キーを発行（例: `VAULTWARDEN_SMTP_PASSWORD`） | tech.md記載済みのキー名をそのまま新規登録 | サービス単位の認証情報分離 | 同一LDAPアカウント（noreply）に対し複数パスワードを管理する手間、要件1.4/2.4（重複登録禁止）に反する | 不採用 |

## Design Decisions

### Decision: SMTP認証情報はAuthentikと同一Infisicalキーを参照する
- **Context**: tech.md には `VAULTWARDEN_SMTP_PASSWORD` という未使用キー名が既に記載されているが、実際にはAuthentikが使う `NOREPLY_SMTP_PASSWORD` と同じ `noreply` アカウントを使うのが目的。
- **Alternatives Considered**:
  1. tech.md記載通り `VAULTWARDEN_SMTP_PASSWORD` を新規発行 — 同一アカウントに2つのパスワードが必要になり運用が煩雑
  2. `NOREPLY_SMTP_PASSWORD` をそのまま再利用 — 単一の認証情報源
- **Selected Approach**: 2を採用。Vaultwarden/Directus の ExternalSecret は `remoteRef.key: NOREPLY_SMTP_PASSWORD` を参照する。
- **Rationale**: ユーザー要求（「authentikの設定を流用」）、および要件1.4/2.4（重複登録禁止）に直接合致する。
- **Trade-offs**: tech.md上の `VAULTWARDEN_SMTP_PASSWORD` という記載は実体が無いまま残っていたため、ドキュメント更新時に削除し、実際の参照先（`NOREPLY_SMTP_PASSWORD`の再利用）を明記する。
- **Follow-up**: `steering/tech.md` のVaultwardenシークレット一覧行を更新（Requirement 3.1で対応）。

### Decision: Directus の `EMAIL_SMTP_SECURE` / `EMAIL_SMTP_IGNORE_TLS` は未設定のままにする
- **Context**: 587番ポート + STARTTLSという構成はAuthentikと同条件だが、Directus(Nodemailer)では `secure=true` が暗黙的TLS(465相当)を意味するため、誤設定すると接続失敗する。
- **Alternatives Considered**:
  1. `EMAIL_SMTP_SECURE=true` を設定 — 587番ポートでは不適合、接続失敗リスク
  2. 未設定（デフォルトfalse、STARTTLS自動アップグレード）— Authentikと同等の挙動
- **Selected Approach**: 2を採用。
- **Rationale**: Nodemailerの仕様上、`secure=false` + サーバー側STARTTLS対応 で自動的にTLSアップグレードされるため、追加設定不要。
- **Trade-offs**: なし（設定項目を増やさない方がシンプル）。
- **Follow-up**: 動作確認時（Requirement 3.3）に実際のTLSアップグレードをログで確認する。

## Risks & Mitigations
- 同一 `noreply` アカウントへの依存集中（Authentik + Vaultwarden + Directus） — パスワードローテーション時は3アプリすべての再起動が必要になる点を運用手順に明記し、`directus-secrets` にも reloader アノテーションを追加して自動化する。
- Directus の `EMAIL_SMTP_SECURE` 設定誤り — 上記Design Decisionで未設定方針とし、Requirement 3.3で送信テストを必須化して検知する。
- tech.md記載のキー名(`VAULTWARDEN_SMTP_PASSWORD`)と実装の不一致 — Requirement 3.1のドキュメント同期で解消する。

## References
- [Vaultwarden .env.template](https://github.com/dani-garcia/vaultwarden/blob/main/.env.template) — SMTP関連環境変数の正式な変数名・既定値
- [Directus Email Configuration](https://directus.com/docs/configuration/email) — EMAIL_SMTP_*環境変数の仕様
