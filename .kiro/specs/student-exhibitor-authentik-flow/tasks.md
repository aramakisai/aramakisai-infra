# Implementation Plan

design.md の File Structure Plan（3コンポーネント3ファイル + スクリプト1本）に対応する。Provisioning（`authentik_student_exhibitor_provisioning.tf`）と Password Setup Flow（`authentik_student_exhibitor_flow.tf`）はファイル・コンポーネント境界が独立しているため並行実装可能。Recovery Email Sender Script は Foundation で発行するAPIトークンにのみ依存し、他コンポーネントの実装完了を待たずに着手できる。

- [x] 1. 基盤整備: CSVスキーマとメール送信用サービスアカウントの準備
- [x] 1.1 学生団体一覧CSVのスキーマとサンプルデータを準備する
  - `terraform/data/student_exhibitors.csv` を新規作成し、`team_name` / `email` の2列構成とする
  - `email` 列がCSV内で一意になるよう検証用のサンプル行を用意する
  - Observable: `csvdecode` で読み込み可能な形式のCSVファイルがリポジトリに存在し、サンプル行がパースエラーなく読み込める
  - _Requirements: 1.1, 1.2_

- [x] 1.2 Recovery Email Sender Script用のAuthentikサービスアカウントを発行する
  - `terraform/authentik_student_exhibitor_recovery_sa.tf` に、対象ユーザーの参照・属性更新・リカバリーメール送信に必要な権限のみを持つRBACロールを定義する（既存 `authentik_vaultwarden_rbac_sync.tf` の RBACロール+サービスアカウント+APIトークンの構成パターンを踏襲）
  - 発行したAPIトークンをInfisicalへ登録する
  - Observable: `terraform apply` 後、発行されたAPIトークンでAuthentik APIへの認証済みリクエスト（自ユーザー情報取得等）が成功する
  - _Requirements: 3.1_

- [x] 2. Core: 学生団体アカウントの一括作成（Student Exhibitor Provisioning）
- [x] 2.1 (P) CSV駆動のユーザー一括作成ロジックを実装する
  - `terraform/authentik_student_exhibitor_provisioning.tf` に、CSVの各行を `email` をキーとするマップへ変換し `authentik_user` を `for_each` 生成する定義を追加する
  - 各ユーザーは `username = email`、`type = internal`、既存 `authentik_group.student_exhibitor` への所属、パスワード未指定（未設定状態）とする
  - Observable: `terraform plan` でCSVの行数分の `authentik_user` 作成が計画され、各リソースが `student_exhibitor` グループ・`internal` タイプ・パスワード未設定であることを確認できる
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1_
  - _Boundary: Student Exhibitor Provisioning_

- [x] 2.2 一括作成の冪等性・部分失敗耐性を検証する
  - 同一CSVで `terraform apply` を再実行し、既存ユーザーに対する差分が発生しないことを確認する
  - CSVに重複メールアドレスを含む行を一時的に追加し、`terraform plan` がエラーとして検出することを確認する
  - Observable: 再apply時の差分なしログと、重複メールアドレス検出時のplanエラーメッセージの両方が確認できる
  - _Requirements: 1.5, 1.6_

- [x] 3. Core: パスワード設定フロー（Student Exhibitor Password Setup Flow）
- [x] 3.1 (P) 専用フロー本体とステージを定義する
  - `terraform/authentik_student_exhibitor_flow.tf` に、フロー本体（`student-exhibitor-password-setup`）と、送信専用のメールステージ（識別ステージなし、`token_expiry` 設定）→パスワード確認付きプロンプトステージ（`authentik_stage_prompt`、password/password_repeatの2フィールド、`validation_policies` に既存パスワードポリシーをバインド）→ユーザー書き込みステージ（`authentik_stage_user_write`、`user_creation_mode = never_create`）→自動ログインステージの4ステージ・バインディングを追加する
  - `authentik_stage_password`（既存パスワード検証専用ステージ）は使用しない。実機検証（2026-08-26）でフォーム非表示のままステージがスキップされ、パスワード未設定で自動ログインへ抜ける不具合を確認済み。`invitation-enrollment` の `authentik_stage_prompt.enrollment_user_password` と同型のパターンを踏襲する
  - Observable: `terraform plan` でフロー・4ステージ・4バインディングが計画され、プロンプトステージに既存パスワードポリシーが `validation_policies` としてバインドされていることを確認できる
  - _Requirements: 3.2, 4.1, 4.2, 4.3, 4.4, 6.1_
  - _Boundary: Student Exhibitor Password Setup Flow_

- [x] 3.2 認証済みユーザーによるフロー誤操作防止ポリシーを束縛する
  - 既存 `authentik_policy_expression.deny_enrollment_if_authenticated`（`terraform/authentik_enrollment.tf`）を、新規フロー本体に対する `authentik_policy_binding`（order=0）として束縛する
  - Observable: `terraform plan` で新規 `authentik_policy_binding` が計画され、既存ポリシーリソースへの変更（diff）が発生しないことを確認できる
  - _Requirements: 5.1_
  - _Boundary: Student Exhibitor Password Setup Flow_

- [x] 4. Core: パスワード設定メール送信（Recovery Email Sender Script）
- [x] 4.1 (P) 未送信ユーザーへのメール送信スクリプトを実装する
  - `student_exhibitor` グループ内で送信済み属性が未設定のユーザーを抽出し、各ユーザーに対し `recovery_email` APIを呼び出す処理を実装する
  - 送信成功後、当該ユーザーへ送信済みを示す属性（送信日時）を記録する処理を実装する
  - Observable: スクリプト実行後、対象ユーザー全員へのメール送信が完了し、各ユーザーの属性に送信済み記録が反映されている
  - _Requirements: 3.1_
  - _Boundary: Recovery Email Sender Script_
  - _Depends: 1.2_

- [x] 4.2 スクリプトの冪等性・部分失敗耐性を検証する
  - 同一グループに対しスクリプトを再実行し、送信済みユーザーがスキップされ再送されないことを確認する
  - 1ユーザーへの送信が失敗するケース（無効なユーザーID等）を模擬し、他ユーザーへの送信処理が継続されることを確認する
  - Observable: 再実行時のスキップログと、部分失敗時に他ユーザーへの送信が完了したことを示す実行結果の両方が確認できる
  - _Requirements: 3.1_
  - _Boundary: Recovery Email Sender Script_

- [ ] 5. Integration: 一括作成からDirectusログインまでの結合確認
- [x] 5.1 一括作成・メール送信・パスワード設定・自動ログインの一連の流れを確認する
  - テスト用の団体データをCSVに追加し `terraform apply` でアカウントを作成する
  - Recovery Email Sender Scriptを実行しテストアカウント宛にメールを送信する
  - メール内リンクを開いてパスワードを設定し、設定完了後に自動的にログイン状態となりDirectusへアクセスできることを確認する
  - Directus側でテストアカウントに `student_exhibitor` ロールが付与されていることを確認する
  - Observable: テストアカウントがパスワード設定完了後に追加操作なしでDirectusへログインでき、付与ロールが `student_exhibitor` であることを確認できる
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 3.1, 4.1, 4.4, 6.1_
  - _Depends: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 4.1, 4.2_

- [ ] 6. Validation: エラーケースと誤操作防止の確認
- [x] 6.1 パスワード未設定アカウントで通常ログインが拒否されることを確認する
  - パスワード設定前のテストアカウントで、通常のログイン画面からログインを試み拒否されることを確認する
  - Observable: パスワード未設定状態でのログイン試行がエラーとなり、ログインが成立しないことを確認できる
  - _Requirements: 2.2_

- [x] 6.2 認証済みユーザーがパスワード設定リンクを開いた場合の拒否メッセージを確認する
  - 既にログイン中のセッションでテストアカウント宛のパスワード設定リンクを開き、ログアウトを促すメッセージが表示され処理が拒否されることを確認する
  - Observable: 認証済み状態でのリンクアクセス時にパスワード設定が実行されず、拒否メッセージが表示される
  - _Requirements: 5.1_

- [x] 6.3 パスワードポリシー違反時のエラー表示を確認する
  - パスワード設定フォームにポリシーを満たさないパスワードを入力し送信する
  - Observable: エラーメッセージが表示され、パスワード設定が完了しない（アカウントがログイン不可状態のまま維持される）
  - _Requirements: 4.2_

- [x] 6.4 リンク無効・期限切れ時のエラー表示を確認する
  - 期限切れまたは無効なパスワード設定リンクを開き、エラーメッセージが表示されることを確認する
  - Observable: 無効・期限切れリンクへのアクセス時にパスワード設定フォームへ進めず、エラーメッセージが表示される
  - _Requirements: 3.2, 4.3_
