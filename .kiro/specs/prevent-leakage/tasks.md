# Implementation Plan - Prevent Confidential Information Leakage

## Tasks

- [x] 1. 機密情報検知カスタムスクリプトの作成
  - `scripts/check-confidential-info.py` を新規作成する。
  - 以下の仕様に基づき、Pythonで検知ロジックを実装する：
    - 環境変数 `$USER`, `$USERNAME`, `$HOME`, `$USERPROFILE` 等から、動的に「現在の開発者ユーザー名」および「現在のホームディレクトリ」を取得する。
    - コミット対象ファイルの差分またはファイル内容を走査し、取得した個人ユーザー名を含む絶対パス（例: `/home/<user>/...` 等）が含まれている場合にエラーとする。
    - 一般的なシステム共通パス（例: `/etc/`, `/tmp/`, `/var/` 等）や、ソースコード中のライブラリ・モジュールインポートのパス記述等は検知から除外する。
    - 正規表現 `(?i)\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b` でメールアドレスを検出する。
    - テストドメイン（`example.com`, `example.org`, `test.com`等）や GitHub の `noreply` メールアドレス、プロジェクト公開用のパブリックなメールアドレスはホワイトリストに登録し、検知対象外とする。
    - 行末に `# confidential:allow` または `// confidential:allow` が記述されている行は、検知をバイパス（スキップ）する。
    - エラー検知時は、ファイル名、行番号、該当箇所を表示し、ステータスコード `1` で終了する。
  - _Requirements: 1, 2, 3_
  - _Boundary: scripts/check-confidential-info.py_

- [x] 2. pre-commit 設定の更新
  - `.pre-commit-config.yaml` にカスタムフックを統合する。
  - `local` リポジトリの hooks に以下を追加する：
    - `id: check-confidential-info`
    - `name: Check Confidential Info`
    - `entry: uv run python scripts/check-confidential-info.py`
    - `language: system`
    - `files: \.(py|sh|yaml|yml|json|md|txt|tf|tfvars|cfg)$`
    - `pass_filenames: true`
  - _Requirements: 1, 2_
  - _Boundary: .pre-commit-config.yaml_

- [x] 3. 動作検証およびバイパス確認
  - 以下の検証用ファイルを作成し、`pre-commit run --files <ファイル名>` で動作を確認する：
    - 正常系:
      - 違反情報を含まないファイル（パスすること）
      - 除外対象のダミーメールアドレス（`test@example.invalid` など）を含むファイル（パスすること）
      - 違反情報を含むが、行末に `# confidential:allow` コメントがあるファイル（パスすること）
    - 異常系:
      - 自身のローカル絶対パス（例: `/home/<user>/` 以下）をハードコードしたファイル（ブロックされること）
      - 非許可のメールアドレス（例: `personal-user@non-allowlisted.invalid`）をハードコードしたファイル（ブロックされること） <!-- confidential:allow -->
  - 検証完了後、検証用ファイルを削除する。
  - _Requirements: 1, 2, 3_
  - _Boundary: Verification_
