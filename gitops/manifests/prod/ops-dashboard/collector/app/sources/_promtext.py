import re
from typing import Iterable, Mapping

_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+\d+)?$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
_UNESCAPE = re.compile(r'\\(.)')


def _unescape(s: str) -> str:
    return _UNESCAPE.sub(lambda m: "\n" if m.group(1) == "n" else m.group(1), s)


def parse_samples(text: str, names: Iterable[str]) -> Mapping[str, list[tuple[Mapping[str, str], float]]]:
    """Prometheus テキスト形式から、指定した名前のサンプル (ラベルと値) を返す。NaN・不正な値の行は除く。"""
    out = {n: [] for n in names}
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m or m.group(1) not in out:
            continue
        try:
            v = float(m.group(3))
        except ValueError:
            continue
        if v != v:
            continue
        labels = {k: _unescape(x) for k, x in _LABEL.findall(m.group(2) or "")}
        out[m.group(1)].append((labels, v))
    return out
