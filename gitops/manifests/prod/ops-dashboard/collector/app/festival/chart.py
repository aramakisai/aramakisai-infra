"""当日監視のグラフ。入力の値だけから SVG を作り、SQLite と時計は読まない。"""
from datetime import datetime, timedelta
from typing import Sequence

from festival.labels import label
from model import MetricUnit
from render.components import esc
from render.svg import _nice_max

Point = tuple[datetime, float]

LINE = 'fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"'


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _path(cells: dict[int, tuple[float, float]], style: str) -> str:
    """分の番号 → (x, y)。番号が連続する区間ごとに 1 本の線にする。"""
    runs: list[list[tuple[float, float]]] = []
    prev = None
    for i in sorted(cells):
        if prev != i - 1:
            runs.append([])
        runs[-1].append(cells[i])
        prev = i
    # 1 点だけの区間は、丸い端の長さ 0 の線分にしないと描かれない
    d = "".join("M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in (r if len(r) > 1 else r * 2)) for r in runs)
    return f'<path d="{d}" {LINE} style="{style}"/>'


def sparkline(points: Sequence[Point], end_minute: datetime) -> str:
    start = _minute(end_minute) - timedelta(minutes=59)
    vals: dict[int, float] = {}
    for t, v in points:
        i = int((_minute(t) - start).total_seconds() // 60)
        if 0 <= i < 60:
            vals[i] = v
    if not vals:
        return ""
    top = _nice_max(max(vals.values()))
    cells = {i: (600 * i / 59, 98 - 96 * v / top) for i, v in vals.items()}
    return ('<svg viewBox="0 0 600 100" preserveAspectRatio="none" aria-hidden="true" class="spark">'
            + _path(cells, "stroke:currentColor;vector-effect:non-scaling-stroke") + "</svg>")


def format_axis(value: float, unit: MetricUnit) -> str:
    v = f"{value * 100:.0f}" if unit == "ratio" else f"{value:g}"
    return label(f"fest.unit.{unit}", value=v)


PL, PT, PW, PH = 56, 8, 400, 188


def day_chart(points: Sequence[Point], start: datetime, end: datetime, unit: MetricUnit,
              warn: float | None, crit: float | None, label_text: str) -> str:
    start = _minute(start)
    span = max((end - start).total_seconds(), 60)
    vals = {}
    for t, v in points:
        if start <= t <= end:
            vals[int((_minute(t) - start).total_seconds() // 60)] = v
    if not vals:
        return f'<div class="chart-empty">{esc(label("fest.detail.chart_empty"))}</div>'
    top = _nice_max(max([*vals.values(), *(x for x in (warn, crit) if x is not None)]))

    def y(v):
        return PT + PH * (1 - v / top)

    parts = []
    for i in range(3):
        gy = y(top * i / 2)
        parts.append(f'<line x1="{PL}" x2="{PL + PW}" y1="{gy:.1f}" y2="{gy:.1f}" style="stroke:var(--line)"/>'
                     f'<text x="{PL - 4}" y="{gy + 4:.1f}" text-anchor="end" font-size="12">'
                     f'{esc(format_axis(top * i / 2, unit))}</text>')
    hours = span / 3600
    step = 1 if hours <= 8 else 2 if hours <= 16 else 3
    t = start
    while t <= end:
        tx = PL + PW * (t - start).total_seconds() / span
        # 左端の文字は縦軸の「0」の文字と重なるため、端の目盛は内側へ寄せる
        anchor = "start" if tx <= PL else "end" if tx >= PL + PW else "middle"
        parts.append(f'<text x="{tx:.1f}" y="{PT + PH + 18}" text-anchor="{anchor}" font-size="12">'
                     f'{t.hour:02d}:00</text>')
        t += timedelta(hours=step)
    cells = {i: (PL + PW * i * 60 / span, y(v)) for i, v in vals.items()}
    parts.append(_path(cells, "stroke:var(--fg)"))
    lines = [(v, key, dash, color) for v, key, dash, color in
             ((warn, "fest.detail.chart_warn", "6 4", "var(--warn)"),
              (crit, "fest.detail.chart_crit", "2 3", "var(--crit)")) if v is not None]
    ty = [y(v) + 4 for v, *_ in lines]
    if len(lines) == 2 and abs(ty[0] - ty[1]) < 14:
        up = ty[0] >= ty[1]  # 上側 (y が小さい方)。同じ高さなら異常側を上げる
        ty[up] -= 14
        if ty[up] < 12:  # viewBox の上端で文字が切れないよう、下側を動かして間隔を保つ
            ty[up] = 12
            ty[not up] = 26
    for (v, key, dash, color), tyy in zip(lines, ty):
        ly = y(v)
        parts.append(f'<line x1="{PL}" x2="{PL + PW}" y1="{ly:.1f}" y2="{ly:.1f}" stroke-width="1.5" '
                     f'stroke-dasharray="{dash}" style="stroke:{color}"/>'
                     f'<text x="{PL + PW + 4}" y="{tyy:.1f}" font-size="12" style="fill:{color}">'
                     f'{esc(label(key, value=format_axis(v, unit)))}</text>')
    return (f'<svg viewBox="0 0 528 220" role="img" aria-label="{esc(label("fest.detail.chart_aria", label=label_text))}" '
            f'class="daychart">{"".join(parts)}</svg>')
