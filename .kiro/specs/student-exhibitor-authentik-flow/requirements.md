# Requirements Document

## Project Description (Input)
学生団体登録用authentik flowを作成したい

## Introduction
学生団体（出展団体）アカウントは、実行委員会担当者が団体一覧（CSV等）をもとに事前に一括作成し、団体代表者は送付されたメールのリンクからパスワードのみを設定してアカウントを有効化する方式とする。ユーザー名はメールアドレスをそのまま使用し、代表者自身による団体情報の入力（自己登録）は行わない。作成したアカウントは、Directus の `ROLE_MAPPING`（`gitops/manifests/prod/directus/deployment.yaml`）が参照するAuthentikグループ `student_exhibitor`（`terraform/authentik_apps.tf` で定義済み）へ自動的に所属させる。

## Boundary Context (Optional)
- **In scope**: 実行委員会担当者による学生団体アカウントの一括事前作成（ユーザー名=メールアドレス、`student_exhibitor` グループ付与を含む）、パスワード設定用メールの送信、代表者によるパスワード設定のみの簡易フロー、認証済みユーザーによる誤操作防止
- **Out of scope**: 代表者自身による団体情報・プロフィールの自己入力（委員会側の事前登録で完結するため不要）、既存 `invitation-enrollment` フロー自体の変更、Directus側のロール・権限定義（`migrations-configmap.yaml`）の変更、学生団体一覧の収集・審査プロセス自体
- **Adjacent expectations**: Authentikの recovery / password-reset 系フローパターン（既存ユーザーに対するパスワード設定リンク送付）を踏襲する。`terraform/authentik_enrollment.tf` の招待制自己登録パターンとは別方式であり、`invitation` ステージは使用しない。一括作成・メール送信の具体的な実装方式（スクリプト構成・API呼び出し方法等）はdesignフェーズで確定するが、冪等性（同一入力での再実行安全性）と部分失敗耐性（一部レコードの失敗が他レコードへ波及しない）を満たすロバストな方式を選定・確定させること。

## Requirements

### Requirement 1: 学生団体アカウントの一括事前作成
**Objective:** 実行委員会担当者として、学生団体一覧をもとにアカウントをまとめて事前作成したい、それにより団体ごとに個別の招待・入力対応を行う手間を省くため

#### Acceptance Criteria
1. The system shall 実行委員会担当者が学生団体一覧（団体名・代表者メールアドレス等）をもとにAuthentikユーザーアカウントを一括作成できる手段を提供する。
2. When アカウントが一括作成される, the system shall 各ユーザーのユーザー名に登録メールアドレスをそのまま使用する。
3. When アカウントが一括作成される, the system shall 各ユーザーをAuthentikグループ `student_exhibitor` へ自動的に追加する。
4. The system shall 一括作成するユーザーの `user_type` を `internal` に設定する。
5. If 一括作成対象の一部レコードでエラーが発生する, then the system shall 該当レコードのみをエラーとして記録し、残りのレコードの処理を継続する。
6. When 同一の学生団体一覧データに対して一括作成処理が再実行される, the system shall 既に作成済みのアカウントを重複作成しない。

### Requirement 2: パスワード未設定アカウントのログイン不可状態
**Objective:** 実行委員会担当者として、パスワード設定が完了するまでアカウントをログイン不能な状態にしたい、それにより第三者が代表者になりすましてログインすることを防ぐため

#### Acceptance Criteria
1. When アカウントが一括作成される, the system shall パスワードが未設定の状態でアカウントを作成する。
2. While 対象アカウントのパスワードが未設定である, the Student Exhibitor Password Setup Flow shall 通常のログインを許可しない。

### Requirement 3: パスワード設定用メールの送信
**Objective:** 学生団体代表者として、パスワード設定用のリンクをメールで受け取りたい、それにより自分でアカウントを有効化できるようにするため

#### Acceptance Criteria
1. When 実行委員会担当者がアカウントの一括作成を完了する, the system shall 各アカウントの登録メールアドレス宛にパスワード設定用リンクを含むメールを送信する。
2. The system shall パスワード設定用リンクに有効期限を設ける。

### Requirement 4: パスワード設定フロー
**Objective:** 学生団体代表者として、メールのリンクから自分のパスワードだけを設定したい、それによりアカウント登録の手間を最小限にするため

#### Acceptance Criteria
1. When 代表者がパスワード設定用リンクを開く, the Student Exhibitor Password Setup Flow shall パスワードおよびパスワード確認の入力のみを要求する。
2. If 入力されたパスワードが既定のパスワードポリシーを満たさない, then the Student Exhibitor Password Setup Flow shall エラーを表示し設定を完了させない。
3. If パスワード設定用リンクが無効または期限切れである, then the Student Exhibitor Password Setup Flow shall パスワード設定を拒否しエラーメッセージを表示する。
4. When パスワード設定が正常に完了する, the Student Exhibitor Password Setup Flow shall 対象アカウントをログイン可能な状態にする。

### Requirement 5: 認証済みユーザーによるフロー誤操作の防止
**Objective:** 実行委員会担当者として、既にログイン中のユーザーが誤ってパスワード設定フローに入り既存アカウントの情報を上書きすることを防ぎたい、それにより過去に発生した既存インシデント（2026-06-22、`invitation-enrollment` での類似事案）と同種の事故を再発させないため

#### Acceptance Criteria
1. If フローへのアクセス時点でリクエストユーザーが既に認証済みである, then the Student Exhibitor Password Setup Flow shall 処理を拒否しログアウトを促すメッセージを表示する。

### Requirement 6: パスワード設定完了後の自動ログイン
**Objective:** 学生団体代表者として、パスワード設定完了後すぐにDirectusへアクセスしたい、それにより設定後に再度ログイン操作を行う手間を省くため

#### Acceptance Criteria
1. When パスワード設定ステージが正常に完了する, the Student Exhibitor Password Setup Flow shall ユーザーを自動的にログイン状態にする。
