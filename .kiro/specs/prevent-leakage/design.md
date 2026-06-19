# Design Document - Prevent Confidential Information Leakage in Pre-commit

## Overview
本ドキュメントは、ローカル環境の絶対パス（フルパス）や個人メールアドレスなどの機密情報に近い情報が、誤ってコミットされリモートレポジトリにプッシュされるのを防ぐための `pre-commit` カスタム検証の設計仕様です。

### Goals
- 開発者がローカル環境固有の絶対パス（個人用ホームディレクトリ等）をコードや設定ファイルに直書きしたままコミットするのをブロックする。
- 開発者個人のメールアドレスや、外部に公開すべきでない組織内メールアドレスのハードコーディングをコミット時に検知しブロックする。
- 開発者の個人ユーザー名などの機密情報をリポジトリ内の共通設定ファイル（例: `gitleaks.toml`）に直接ハードコードせず、実行環境から動的に検出パターンを構築して検知する仕組みを構築する。
- テストコードやサンプルデータ、ドキュメントなど、意図的に特定のパスやメールアドレスを記述したい場合に、明示的に検知をバイパス（ホワイトリスト化）する手段を提供する。

### Non-Goals
- 過去のGitコミット履歴の改変および機密情報の削除。
- 既存の `gitleaks` による汎用シークレット（APIキーやプライベートキー等）検知機能の代替（gitleaksはそのまま存続し、本機能と併用する）。

---

## Boundary Commitments

### This Spec Owns
- [scripts/check-confidential-info.py](../../scripts/check-confidential-info.py) — 独自の機密情報検知ロジック（動的絶対パス・メールアドレス）を実装するPythonスクリプト。
- [.pre-commit-config.yaml](../../.pre-commit-config.yaml) — カスタム検証スクリプトを `pre-commit` フックとして統合。
- `.kiro/specs/prevent-leakage/` 配下の仕様管理ファイルの更新。

### Out of Boundary
- pre-commit 自体のインストールや環境設定は、開発者が各々のローカル環境ですでに完了しているものと想定する。

---

## File Structure Plan

### New Files
- [scripts/check-confidential-info.py](../../scripts/check-confidential-info.py) — 各種機密情報の検知（動的パス、メールアドレススキャン、インラインのバイパス処理）を行うためのカスタムスキャンスクリプト。

### Modified Files
- [.pre-commit-config.yaml](../../.pre-commit-config.yaml) — `local` レポジトリ配下にカスタムフック `check-confidential-info` を追加。
- [.kiro/specs/prevent-leakage/spec.json](spec.json) — 承認状態およびフェーズを更新。

---

## Architecture & Implementation Details

### 1. カスタム検証スクリプトの設計 (`check-confidential-info.py`)
`uv run python` で実行可能なPythonスクリプトとして実装します。

#### A. 動的絶対パスの検出
開発者の個人名を含むローカルパスを共有の定義ファイルに書かずに検知するため、実行時の環境変数から動的にパターンを生成します。
- **取得する情報**:
  - `whoami` または `os.getlogin()`, 環境変数 `USER`, `USERNAME` 等から「現在のユーザー名」を取得。
  - 環境変数 `HOME` または `USERPROFILE` から「現在のホームディレクトリ」を取得。
- **検出パターン**:
  - `HOME` の値（例: `/home/<user>` や `/Users/<user>` 等）がコード内で絶対パスの一部として現れた場合。
  - Windows環境における `C:\Users\<user>` 等のパス。
  - ※ただし、標準的なシステム共通パス（例: `/etc/`, `/var/`, `/tmp/` 等）や、プログラム実行時の動的パスの組み立て（例: `os.path.join` などの一般的なコードパターン）は除外します。

#### B. メールアドレスの検出
一般的なメールアドレスを正規表現で検出します。
- **検出用正規表現**:
  - `(?i)\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b`
- **デフォルト許可リスト (Allowlist)**:
  - スクリプト内にデフォルトで除外すべきドメインやアドレスの正規表現リストを持ちます。
    - サンプル・ダミードメイン: `example.com`, `example.org`, `example.net`, `test.com` 等
    - GitHub/Git提供のアドレス: `noreply.github.com`, `users.noreply.github.com` 等
    - その他、プロジェクトで公式に公開しているメールアドレス

#### C. インラインコメントによるバイパス (`# confidential:allow`)
検証を個別に許可するための仕組みをサポートします。
- 行の末尾に `# confidential:allow` (YAMLやPython等のコメント) または `// confidential:allow` (JSやJSONC等) が存在する場合、その行の検知をスキップします。

### 2. pre-commit への統合
`.pre-commit-config.yaml` にローカルフックとして以下を追加します。

```yaml
  - repo: local
    hooks:
      # （既存のフック...）
      - id: check-confidential-info
        name: Check Confidential Info
        entry: uv run python scripts/check-confidential-info.py
        language: system
        files: \.(py|sh|yaml|yml|json|md|txt|tf|tfvars|cfg)$
        pass_filenames: true
```

---

## Testing Strategy

### 1. 正常系テスト（コミット成功）
- 機密情報を含まない通常のコード変更が、`pre-commit run --all-files` で正常にパスすること。
- ダミーメールアドレス（例: `user@example.invalid`）や、インラインコメント `# confidential:allow` が付与された行がスキャンをパスすること。

### 2. 異常系テスト（コミットブロック）
- 自身のホームディレクトリを含む絶対パスを記述したファイルを作成し、`pre-commit` で正しくブロックされること。
- 非許可のメールアドレス（例: `personal-user@non-allowlisted.invalid` など）を記述したファイルを作成し、`pre-commit` で正しくブロックされること。 <!-- confidential:allow -->
- エラー出力時に、どのファイルのどの行でどのようなポリシー違反が検出されたかが分かりやすく出力されること。
