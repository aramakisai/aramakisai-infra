#!/usr/bin/env python3
import sys
import os
import re
import getpass
import subprocess
try:
    import pwd
except ImportError:
    pwd = None

# メールアドレス検知用正規表現
# 日本語文字（Unicodeの\wにマッチする）との境界での検知漏れを防ぐため、\bの代わりに否定戻り読み/先読みを使用する
EMAIL_REGEX = re.compile(r'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])')

# 許可されるメールアドレス・ドメインのホワイトリスト (正規表現)
# 先頭(^)と末尾($)を明示し、re.match による効率的な判定を行う
EMAIL_ALLOWLIST = [
    re.compile(r'^[^@]+@example\.(com|org|net)$', re.IGNORECASE),
    re.compile(r'^[^@]+@test\.com$', re.IGNORECASE),
    re.compile(r'^[^@]+@local$', re.IGNORECASE),
    re.compile(r'^[^@]+@domain\.com$', re.IGNORECASE),
    re.compile(r'^[^@]+@users\.noreply\.github\.com$', re.IGNORECASE),
    re.compile(r'^[^@]+@noreply\.github\.com$', re.IGNORECASE),
    re.compile(r'^[^@]+@aramakisai\.com$', re.IGNORECASE),      # Organization's own domain (intentional config)
    re.compile(r'^[^@]+@aramakisai\.invalid$', re.IGNORECASE),  # Internal sample domain
    re.compile(r'^[^@]+@example\.invalid$', re.IGNORECASE),     # External sample domain
    re.compile(r'^git@github\.com$', re.IGNORECASE),           # Git SSH URL
]

# インラインでチェックをバイパスするためのコメントパターン
BYPASS_COMMENT = "confidential:allow"

def get_git_email():
    """Gitの設定からユーザーのメールアドレスを取得する"""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            check=True
        )
        email = result.stdout.strip()
        if email:
            return email
    except Exception:
        pass
    return None

def get_current_user_and_home():
    """現在のユーザー名とホームディレクトリを取得する"""
    username = os.environ.get("USER") or os.environ.get("USERNAME")
    if not username and pwd:
        try:
            username = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            pass
    if not username:
        try:
            username = os.getlogin()
        except Exception:
            pass
    if not username:
        try:
            username = getpass.getuser()
        except Exception:
            pass

    home = os.path.expanduser("~")
    return username, home

def is_email_allowed(email):
    """メールアドレスが許可リストに含まれているか判定する"""
    for pattern in EMAIL_ALLOWLIST:
        if pattern.match(email):
            return True
    return False

def scan_file(filepath, username, home, git_email):
    """ファイルをスキャンして機密情報（パス、メール）を検知する"""
    errors = []

    if os.path.abspath(filepath) == os.path.abspath(__file__):
        return errors

    # ホームディレクトリのパスを正規表現用にコンパイル
    # 区切り文字（スラッシュ/バックスラッシュ）とワード境界を考慮した厳密な境界判定
    home_normalized = home.replace('\\', '/')
    home_escaped = re.escape(home_normalized)
    # パスが home_normalized そのもの、またはその直後に区切り文字が続く場合のみマッチさせる
    # URL (例: https://example.com/home/user) などの誤検知を防ぐため、直前に英数字やドットがないことを確認
    path_pattern = re.compile(rf"(?<![A-Za-z0-9\.]){home_escaped}(?:[/\\]|\b)", re.IGNORECASE)

    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line_num, line in enumerate(f, 1):
                if BYPASS_COMMENT in line:
                    continue

                # 1. ホームディレクトリ絶対パスの検知
                # パフォーマンス向上のため、まずは高速な部分一致チェック(in)を実行
                line_normalized = line.replace('\\', '/')
                if home_normalized in line_normalized:
                    # 部分一致した場合にのみ、正規表現で厳密に境界をチェック
                    if path_pattern.search(line_normalized):
                        errors.append({
                            "line": line_num,
                            "type": "Absolute Path",
                            "content": line.strip(),
                            "detail": "Found home directory path (dynamic check)"
                        })

                # 2. メールアドレスの検知
                # パフォーマンス向上のため、まずは高速な文字チェック(@)を実行
                if '@' in line:
                    emails = EMAIL_REGEX.findall(line)
                    for email in emails:
                        # まずホワイトリスト判定を行う（ホワイトリスト優先）
                        if is_email_allowed(email):
                            continue

                        # 開発者のGit登録メールアドレスと一致した場合
                        if git_email and email.lower() == git_email.lower():
                            errors.append({
                                "line": line_num,
                                "type": "Email Address",
                                "content": line.strip(),
                                "detail": "Found developer's registered Git email address (dynamic check)"
                            })
                            break
                        else:
                            errors.append({
                                "line": line_num,
                                "type": "Email Address",
                                "content": line.strip(),
                                "detail": f"Found non-allowlisted email: {email}"
                            })
                            break

    except Exception:
        pass

    return errors

def main():
    if len(sys.argv) < 2:
        print("Usage: check-confidential-info.py <file1> <file2> ...")
        sys.exit(0)

    username, home = get_current_user_and_home()
    git_email = get_git_email()

    files_to_scan = sys.argv[1:]
    has_errors = False

    for filepath in files_to_scan:
        if not os.path.isfile(filepath):
            continue

        errors = scan_file(filepath, username, home, git_email)
        if errors:
            has_errors = True
            print(f"\033[91m[ERROR] Confidential information detected in: {filepath}\033[0m")
            for err in errors:
                print(f"  Line {err['line']} ({err['type']}): {err['detail']}")
                print(f"    Code: {err['content']}")
                print(f"    -> To allow this line, append '# {BYPASS_COMMENT}' at the end of the line.")
                print()

    if has_errors:
        print("\033[91mCommit blocked. Please remove confidential information or bypass using '# confidential:allow'.\033[0m")
        sys.exit(1)
    else:
        sys.exit(0)

if __name__ == "__main__":
    main()
