"""インライン SVG のグラフ。外部 JS・画像は使わない。同じ数値を details の表でも出す。"""
import math
from typing import Sequence

from render.components import Raw, esc, fmt_int, numbers_details

# 色だけに頼らないよう凡例の文字と数値表を必ず併記する。
PALETTE = ("#2f6fb0", "#d9822b", "#4a9d5b", "#a64ca6", "#c4473d", "#2f9fa6", "#8a7a2c", "#6b6b6b")
W, H = 640, 260
L, R, T, B = 56, 12, 12, 56


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * mag:
            return m * mag
    return 10 * mag


def _wrap(title: str, svg: str, legend: Sequence[str], headers: Sequence[str], rows) -> Raw:
    leg = "".join(f'<li><span class="sw" style="background:{PALETTE[i % len(PALETTE)]}"></span>{esc(n)}</li>'
                  for i, n in enumerate(legend))
    legend_html = f'<ul class="legend">{leg}</ul>' if legend else ""
    return Raw(f'<figure class="chart"><figcaption>{esc(title)}</figcaption>{svg}{legend_html}'
               f'{numbers_details(headers, rows)}</figure>')


def _axes(x_title: str, y_title: str, ymax: float) -> str:
    out = [f'<text x="{L + (W - L - R) / 2}" y="{H - 6}" text-anchor="middle" class="axt">{esc(x_title)}</text>',
           f'<text x="12" y="{T + (H - T - B) / 2}" text-anchor="middle" class="axt" '
           f'transform="rotate(-90 12 {T + (H - T - B) / 2})">{esc(y_title)}</text>']
    for i in range(3):
        y = T + (H - T - B) * (1 - i / 2)
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end" class="axl">{fmt_int(ymax * i / 2)}</text>')
    return "".join(out)


def bar_chart(title: str, x_title: str, y_title: str, categories: Sequence[str],
              series: Sequence[tuple[str, Sequence[float]]]) -> Raw:
    """時系列の積み上げ縦棒。series は (凡例名, categories と同じ長さの数値)。"""
    n = max(len(categories), 1)
    totals = [sum(vals[i] for _, vals in series) for i in range(len(categories))]
    ymax = _nice_max(max(totals, default=0))
    pw, ph = W - L - R, H - T - B
    bw = pw / n
    parts = [_axes(x_title, y_title, ymax)]
    step = max(1, math.ceil(n / 12))
    for i, c in enumerate(categories):
        y = T + ph
        for si, (_, vals) in enumerate(series):
            h = ph * vals[i] / ymax
            if h > 0:
                y -= h
                parts.append(f'<rect x="{L + i * bw + bw * 0.1:.1f}" y="{y:.1f}" width="{bw * 0.8:.1f}" '
                             f'height="{h:.1f}" fill="{PALETTE[si % len(PALETTE)]}"/>')
        if i % step == 0:
            parts.append(f'<text x="{L + i * bw + bw / 2:.1f}" y="{T + ph + 14}" text-anchor="middle" '
                         f'class="axl">{esc(c)}</text>')
    svg = (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}" class="svg">'
           f'{"".join(parts)}</svg>')
    rows = [[c] + [vals[i] for _, vals in series] for i, c in enumerate(categories)]
    return _wrap(title, svg, [n for n, _ in series], [x_title] + [n for n, _ in series],
                 [[r[0]] + [fmt_int(v) for v in r[1:]] for r in rows])


def hbar_chart(title: str, x_title: str, y_title: str, rows: Sequence[tuple[str, float]]) -> Raw:
    """項目別の横棒 (上位 N 件など)。rows は (項目名, 値)。"""
    n = max(len(rows), 1)
    left, rowh = 200, 24
    h = rowh * n + 40
    xmax = _nice_max(max((v for _, v in rows), default=0))
    pw = W - left - R
    parts = [f'<text x="{left + pw / 2}" y="{h - 6}" text-anchor="middle" class="axt">{esc(x_title)}</text>',
             f'<text x="{left - 8}" y="12" text-anchor="end" class="axt">{esc(y_title)}</text>']
    for i, (name, v) in enumerate(rows):
        y = 20 + i * rowh
        w = pw * v / xmax
        parts.append(f'<text x="{left - 8}" y="{y + 13}" text-anchor="end" class="axl">{esc(name[:28])}</text>'
                     f'<rect x="{left}" y="{y}" width="{w:.1f}" height="{rowh - 6}" fill="{PALETTE[0]}"/>'
                     f'<text x="{left + w + 4:.1f}" y="{y + 13}" class="axl">{fmt_int(v)}</text>')
    svg = (f'<svg viewBox="0 0 {W} {h}" role="img" aria-label="{esc(title)}" class="svg">'
           f'{"".join(parts)}</svg>')
    return _wrap(title, svg, [], [y_title, x_title], [[name, fmt_int(v)] for name, v in rows])
