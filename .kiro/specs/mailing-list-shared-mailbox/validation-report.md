# 設計検証レポート (Design Validation Report) — mailing-list-shared-mailbox

## 1. 検証概要 (Executive Summary)

本レポートは、`mailing-list-shared-mailbox` の技術設計（[design.md](file:///.kiro/specs/mailing-list-shared-mailbox/design.md)）について、要件ドキュメント（[requirements.md](file:///.kiro/specs/mailing-list-shared-mailbox/requirements.md)）とのトレーサビリティおよび既存のインフラ構成への適合性を検証した結果をまとめます。

- **検証結果**: **条件付き承認 (Conditionally Approved)**
- **総評**:
  全体として、要件（メーリングリストの共有メールボックス化、LDAPの `memberOf` 属性を用いた動的な受信アクセス制御、送信元なりすまし制限の導入、ユーザー名のみでの認証等）は設計に正しくマッピングされています。特に、要件書で想定されていた「グループの `mail` 属性削除による fan-out 停止」という方針が送信者制限と衝突する点を見破り、`mailListAddress` と `mailListMigrated` という独立した2フラグで関心を分離する設計を採用したことは極めて高く評価できます。また、中間マッピングを排して `memberOf=acl_groups` と直接 Dovecot に渡す簡素化も優れています。
  一方で、以下の改善すべき指摘事項が確認されました。これらに対する対処をタスク（`tasks.md`）の設計や実装フェーズで考慮することを条件として、本設計を承認します。

---

## 2. 要件トレーサビリティ検証 (Requirements Traceability)

各要件（REQ）に対する設計でのカバレッジと評価は以下の通りです。

| 要件 ID | 要件概要 | 設計上の対応コンポーネント | 評価 | 備考 |
|---|---|---|---|---|
| **REQ 1.1 - 1.4** | ML宛メールの直接配送・fan-out停止 | Postfix Recipient Filter (LDAP_QUERY_FILTER_GROUP の否定条件、LDAP_QUERY_FILTER_USER での mailListAddress 制限) | **適合** | 2フラグによる関心分離で、移行済みMLのみ段階的に fan-out を停止し直接配送へ切り替える設計が機能する。 |
| **REQ 1.5** | 個人メール受信廃止 | Postfix Recipient Filter (`mailListAddress=true` を `LDAP_QUERY_FILTER_USER` に追加) | **適合** | 個人ユーザーは `mailListAddress` が付与されないため配送先から除外され、適切にバウンスする。 |
| **REQ 1.6** | 個人ログイン維持 | Personal Login Preservation (`DOVECOT_USER_FILTER` 等は変更なし) | **適合** | 配送先判定（Postfix）とログイン判定（Dovecot）が独立している既存構成を活かし、個人ログインを維持する。 |
| **REQ 2.1 - 2.4** | 共有メールボックスへの受信アクセス制御 | Dovecot ACL & Shared Namespace (`DOVECOT_USER_ATTRS` に `memberOf=acl_groups` をマッピング、`dovecot-acl` ファイルの設置) | **適合** | ログインセッションの `memberOf` を用いて、IMAPアクセス時に動的に権限判定を行う。Discordロール連動による動的な制御を満足する。 |
| **REQ 3.1 - 3.5** | 送信者なりすまし防止・個人送信廃止 | Sender Spoof Protection (`SPOOF_PROTECTION=1` の追加、`LDAP_QUERY_FILTER_SENDERS` 用 `ldap-senders.cf` の新設) | **適合** | MLアドレスからの From 送信をグループメンバーに制限。個人 From の送信は常に拒否。 |
| **REQ 4.1 - 4.4** | ユーザー名のみでの認証 | Username-Only Authentication (Dovecot の `auth_username_format = %n@aramakisai.com` 追記) | **適合** | フルアドレスでのログイン後方互換も維持しつつ、ユーザー名のみでの認証と Postfix での From 照合の整合性を確保。 | # confidential:allow
| **REQ 5.1 - 5.4** | メモリ使用量削減 | Resource Tuning (`ENABLE_AMAVIS=0` による Amavis 停止、実測後の resources リソース調整) | **適合** | Rspamd と重複する Amavis を停止。リソース調整はロールアウト後の実測値をベースにする手順を明確化。 |
| **REQ 6.1 - 6.3** | Roundcube Webmailの継続利用 | Roundcube Continuity (変更なし、疎通確認のみ) | **適合** | Roundcube の OAUTHBEARER 認証設定は既存のまま動作させる前提。 |
| **REQ 7.1 - 7.4** | 段階的な移行と検証 | Phased Migration Runbook (`pr@` 先行移行、4点確認手順の定義、ロールバック手順) | **適合** | 本本環境にステージングがないため、`pr@` をパイロットとして検証する実行可能なランブックが定義されている。 |

---

## 3. 技術的・設計上の指摘事項と改善提案 (Findings & Recommendations)

### 【指摘 1】Shared Namespace の自動検出（IMAP LIST）のための `acl_shared_dict` 設定 of 欠落 (改善提案)
- **現状の設計**: `design.md` では Dovecot ACL plugin と Shared namespace を有効にする設計となっていますが、`acl_shared_dict` 設定について明記されていません。
- **影響**: Dovecot で共有フォルダの自動検出（IMAP クライアントが接続した際に、自分がアクセス可能な `shared/` 配下のメールボックスを LIST コマンド等で自動的に表示する動作）を行うには、通常 `acl_shared_dict`（例: `plugin { acl_shared_dict = file:/var/mail/shared-mailboxes.db }`）の設定が必要です。これが欠落していると、クライアント側で共有フォルダのパス（例: `shared/planning@aramakisai.com`）を手動で「購読 (Subscribe)」しないとフォルダが見えない可能性があり、Requirement 6.2 などの要件を満たしにくくなります。 <!-- confidential:allow -->
- **対策**: `dovecot.cf` のオーバーライド部分に、以下の設定を追加することを推奨します。
  ```
  plugin {
    acl = vfile
    acl_shared_dict = file:/var/mail/shared-mailboxes.db
  }
  ```
  また、`shared-mailboxes.db` の書き込み権限について、Dovecot プロセス（DMS 内の vmail/dovecot ユーザー）が書き込めるディレクトリ上に配置するようパスを調整してください。

### 【指摘 2】`special_result_attribute=member` による LDAP 展開時のネストされたグループとタイムアウト対策 (運用上の考慮)
- **現状の設計**: `ldap-senders.cf` の LDAP クエリでは、既存の `ldap-groups.cf` と同様に `special_result_attribute=member` や `timeout=30` を設定し、グループのメンバー展開を行います。
- **影響**: Authentik グループ内にさらにグループがネスト（入れ子）されている場合、またはメンバー数や LDAP サーバーの負荷が大きい場合、Postfix による LDAP 照会が頻繁に発生し、処理遅延（あるいは最悪の場合はタイムアウト）を引き起こすリスクがあります。
- **対策**:
  1. 送信者制限を行う `ldap-senders.cf` では、再帰的展開が必要なネストされたグループ運用を行わない（Authentik 側の運用ルールとして、MLグループにはユーザーアカウントのみを直接所属させる）ことを運用手順に含める。
  2. 実装検証時、`pr@` の送信検証においてPostfixのログ上でLDAPのクエリ所要時間を監視し、タイムアウトや遅延が発生していないかを評価項目に追加する。

### 【指摘 3】`SPOOF_PROTECTION=1` 導入による他コンポーネント（システム自動送信など）への影響調査の明確化 (検証手順の強化)
- **現状の設計**: `SPOOF_PROTECTION=1` はグローバルに有効化され、Postfix の `smtpd_sender_login_maps` チェックが有効になります。
- **影響**: これにより「認証ユーザー名（SASL）と From アドレスの一致（またはグループ展開による From 使用許可）」が厳密に検証されます。もし他の内部コンポーネント（クラスター内外のアプリケーションなど）が、本メールサーバーを認証なしの SMTP リレー（My Networks 経由など）や、特定のシステム用アドレスから From を偽ってメール送信している場合、送信元なりすまし制限に引っかかって送信エラーが発生するリスクがあります。
- **対策**:
  - DMS 側の `smtpd_sender_login_maps` 制限が適用される対象範囲（My Networks 経由の認証なし送信に対しても `reject_sender_login_mismatch` が働くか否か）を確認する。通常、My Networks (クラスター内部IP) からの送信や `smtpd_recipient_restrictions` の設定順序によっては、送信制限がスキップされることもありますが、設定が厳しすぎるとシステム通知メールが不達になります。
  - 設計の「Phase 0」適用時、ML 以外の送信経路（他アプリのシステム通知などがある場合）で送信エラーが発生しないかを初期検証の項目に明記してください。

---

## 4. 移行・ロールバックプランの評価

`design.md` の `Phased Migration Runbook` は非常に現実的で優れています。
- グローバルな変更（設定パラメータの追加や Amavis の無効化など）を一括デプロイし、各MLメールボックスの切り替えを `mailListMigrated` フラグの Authentik 側トグルで1件ずつ行う設計は、本番環境へのリスクを最小限に抑える上で最善の方法です。
- `pr@` 先行移行による 4 点確認（配送、受信、送信、既存影響）は、不具合時の戻し（ロールバック）も Authentik のフラグを外すだけで済むため、安全性が高いと言えます。

---

## 5. 結論と次のステップ

本設計は**「条件付きで承認」**します。

次のステップとして、上記の指摘事項（特に指摘1の `acl_shared_dict` の追加）をタスク設計（[tasks.md](file:///.kiro/specs/mailing-list-shared-mailbox/tasks.md)）に反映させ、Phase 1 のタスク生成（`/kiro:spec-tasks`）へ進んでください。
