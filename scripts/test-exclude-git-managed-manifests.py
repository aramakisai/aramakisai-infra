#!/usr/bin/env python3
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(__file__), "exclude-git-managed-manifests.py")
REPO = os.path.join(os.path.dirname(__file__), "..")

PASS = 0
FAIL = 0


def assert_true(desc, cond):
    global PASS, FAIL
    if cond:
        print(f"  ✅ {desc}")
        PASS += 1
    else:
        print(f"  ❌ {desc}")
        FAIL += 1


def run(stdin, *paths):
    return subprocess.run(
        [sys.executable, SCRIPT, *paths], input=stdin, capture_output=True, text=True
    )


UPSTREAM = """\
apiVersion: v1
kind: ConfigMap
metadata: {name: argocd-cm, namespace: argocd}
data: {resource.exclusions: upstream-default}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: argocd-cmd-params-cm, namespace: argocd}
---
apiVersion: v1
kind: Secret
metadata: {name: argocd-cm, namespace: argocd}
"""

print("=== exclude-git-managed-manifests ===")
with tempfile.TemporaryDirectory() as d:
    git = os.path.join(d, "cm.yaml")
    with open(git, "w") as f:
        f.write("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: argocd-cm}\n")
    r = run(UPSTREAM, git)
    assert_true("正常終了", r.returncode == 0)
    assert_true("Git 管理の ConfigMap を除外", "upstream-default" not in r.stdout)
    assert_true("管理外の ConfigMap は残す", "argocd-cmd-params-cm" in r.stdout)
    assert_true("同名でも kind が違う Secret は残す", "kind: Secret" in r.stdout)

    r = run(UPSTREAM)
    assert_true("パス指定なしは全件そのまま", "upstream-default" in r.stdout)

    r = run(UPSTREAM, os.path.join(d, "missing.yaml"))
    assert_true("存在しないパスはエラー", r.returncode != 0)

git_dir = os.path.join(REPO, "gitops/manifests/prod/argocd")
r = run(UPSTREAM, os.path.join(git_dir, "argocd-cm.yaml"), os.path.join(git_dir, "argocd-rbac-cm.yaml"))
assert_true("実リポジトリの argocd-cm.yaml で除外される", "upstream-default" not in r.stdout)

print(f"\nPASS={PASS} FAIL={FAIL}")
sys.exit(1 if FAIL else 0)
