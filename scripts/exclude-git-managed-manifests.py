#!/usr/bin/env python3
"""stdin の multi-doc YAML から、引数の YAML ファイルと同じ (kind, name) のリソースを除いて stdout へ出す。

upstream の install.yaml を再適用するとき、GitOps (ArgoCD Application) が正本として
管理する ConfigMap に upstream 既定値が混入するのを防ぐ。
"""
import sys

import yaml


def keys(docs):
    return {(d["kind"], d["metadata"]["name"]) for d in docs if d}


def main(paths):
    managed = set()
    for p in paths:
        with open(p) as f:
            managed |= keys(yaml.safe_load_all(f))
    docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
    yaml.safe_dump_all([d for d in docs if keys([d]).isdisjoint(managed)], sys.stdout)


if __name__ == "__main__":
    main(sys.argv[1:])
