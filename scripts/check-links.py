#!/usr/bin/env python3
import sys
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

def check_markdown_links():
    project_root = Path(__file__).parent.parent.resolve()
    markdown_files = []

    # 再帰的にすべてのMarkdownファイルを収集 (.gitなどの隠しディレクトリは除外)
    for path in project_root.rglob('*.md'):
        if any(part.startswith('.') for part in path.parts):
            continue
        markdown_files.append(path)

    errors = []

    # リンク抽出用の正規表現
    # [テキスト](リンク) をマッチさせる
    link_pattern = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')

    for file_path in markdown_files:
        try:
            content = file_path.read_text(encoding='utf-8')
        except Exception as e:
            print(f"Error reading file {file_path}: {e}", file=sys.stderr)
            continue

        file_dir = file_path.parent

        for line_num, line in enumerate(content.splitlines(), 1):
            for match in link_pattern.finditer(line):
                link_text = match.group(1)
                link_target = match.group(2).strip()

                # 外部リンクやメールリンク、アンカーリンクは除外
                if link_target.startswith(('http://', 'https://', 'mailto:', 'tel:')):
                    continue
                if link_target.startswith('#'):
                    continue

                # URLデコード (スペースや日本語のデコード)
                decoded_target = unquote(link_target)

                # アンカー部分の除去 (例: path/to/file.md#section -> path/to/file.md)
                clean_target = decoded_target.split('#', 1)[0]
                if not clean_target:
                    continue

                # パス解決
                if clean_target.startswith('file:///'):
                    # file:/// 形式の絶対パス
                    # 環境依存の絶対パスプレフィックスを除去して、プロジェクトルートからの相対パスとして解決
                    url_obj = urlparse(clean_target)
                    file_path_str = url_obj.path

                    if sys.platform == 'win32' and file_path_str.startswith('/'):
                        file_path_str = file_path_str[1:]

                    if 'aramakisai-infra' in file_path_str:
                        parts = file_path_str.split('aramakisai-infra', 1)
                        relative_part = parts[1].lstrip('/')
                        resolved_path = (project_root / relative_part).resolve()
                    else:
                        resolved_path = Path(file_path_str).resolve()
                else:
                    # 通常の相対パス
                    resolved_path = (file_dir / clean_target).resolve()

                # 存在チェック
                if not resolved_path.exists():
                    errors.append({
                        'file': file_path.relative_to(project_root),
                        'line': line_num,
                        'target': link_target,
                        'resolved': resolved_path
                    })

    if errors:
        print("❌ Broken links found:", file=sys.stderr)
        for err in errors:
            print(f"  {err['file']}:{err['line']} -> Broken link: '{err['target']}' (Resolved to: {err['resolved']})", file=sys.stderr)
        return False

    print("✅ All internal links are valid!")
    return True

if __name__ == '__main__':
    success = check_markdown_links()
    sys.exit(0 if success else 1)
