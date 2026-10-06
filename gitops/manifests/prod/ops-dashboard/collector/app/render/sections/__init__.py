import importlib
from dataclasses import dataclass
from typing import Callable, Mapping

from model import SourceResult


@dataclass(frozen=True)
class Section:
    """ダッシュボードの 1 節。render は節の本文 HTML (見出しは page.py が付ける) を返す。"""
    anchor: str                 # 例 "billing"。<section id="sec-billing">・ナビ・期間切替のリンク先になる
    nav: str                    # labels のキー (nav.*)
    title: str                  # labels のキー (sec.*)
    source_ids: tuple[str, ...]  # 要確認の集計でこの節へリンクする情報源
    render: Callable[[Mapping[str, SourceResult], Mapping[str, list[str]]], str]


# 節の並び順 (ナビと本文の順) の正本。モジュールがまだ無い節は飛ばす。
# 各 sections/<name>.py はモジュール直下に SECTION = Section(...) を定義する。
MODULES: tuple[str, ...] = (
    "billing", "monitoring", "node", "cluster", "data", "connect",
    "mail", "auth", "falco", "fail2ban", "dmarc",
)


def load_sections(modules=None) -> list[Section]:
    out = []
    for name in MODULES if modules is None else modules:
        full = f"render.sections.{name}"
        try:
            out.append(importlib.import_module(full).SECTION)
        except ModuleNotFoundError as e:
            if e.name != full:
                raise
    return out
