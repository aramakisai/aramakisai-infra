"""全節で共通の描画部品。表示文言は labels.label() だけから引く。"""
import html
import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Sequence
from urllib.parse import urlencode

import model
from model import Item, SourceResult, Status
from render.labels import label

JST = timezone(timedelta(hours=9))


class Raw(str):
    """エスケープ済みの HTML。esc() を素通りする。"""


def esc(v) -> Raw:
    return v if isinstance(v, Raw) else Raw(html.escape("" if v is None else str(v)))


def fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M") if dt else "-"


def fmt_int(n) -> str:
    if n is None:
        return "-"
    return f"{n:,.0f}" if isinstance(n, int) or n == int(n) else f"{n:,.1f}"


def fmt_bytes(n: float | None) -> str:
    """10 進の GB / TB。"""
    if n is None:
        return "-"
    return f"{n / 1e12:,.2f} TB" if n >= 1e12 else f"{n / 1e9:,.1f} GB"


def fmt_ratio(r: float | None) -> str:
    return "-" if r is None else f"{r * 100:.1f}{label('unit.percent')}"


_MARK = {Status.OK: "✔", Status.WARN: "▲", Status.CRIT: "✖", Status.ERROR: "!", Status.STALE: "◷", Status.EMPTY: "–"}


def badge(status: Status) -> Raw:
    # 色だけに頼らず、記号と文字を併記する。
    return Raw(f'<span class="badge s-{status.value}"><span aria-hidden="true">{_MARK[status]}</span> '
               f'{esc(label("status." + status.value))}</span>')


def status_cell(item: Item) -> Raw:
    note = f' <span class="note">{esc(item.note)}</span>' if item.note else ""
    return Raw(f"{badge(item.status)}{note}")


def table(headers: Sequence[str], rows: Sequence[Sequence], num_cols: Sequence[int] = (), empty: str | None = None) -> Raw:
    """セルは esc() を通す。HTML を渡すときは Raw で包む。num_cols は右寄せの列番号。"""
    if not rows:
        return Raw(f'<p class="empty">{esc(empty or label("empty.generic"))}</p>')
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f'<td{" class=num" if i in num_cols else ""}>{esc(c)}</td>' for i, c in enumerate(r)) + "</tr>"
        for r in rows)
    return Raw(f'<div class="tablewrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def numbers_details(headers: Sequence[str], rows: Sequence[Sequence]) -> Raw:
    """グラフと同じ数値を表で見せる折りたたみ。開閉の表記切替は CSS で行う (JS なし)。"""
    return Raw('<details class="numbers"><summary>'
               f'<span class="show">{esc(label("table.show_numbers"))}</span>'
               f'<span class="hide">{esc(label("table.hide_numbers"))}</span></summary>'
               f'{table(headers, rows, num_cols=range(1, len(headers)))}</details>')


def card(title: str, result: SourceResult | None, render_body: Callable[[Sequence[Item]], str],
         source: str | None = None, empty: str | None = None,
         select: Callable[[Item], bool] | None = None) -> Raw:
    """情報源 1 つ分のカード。取得失敗は理由・最終成功・直前の値 (薄く) を出し、正常と取り違えない。

    1 つの情報源を複数カードに分けるときは select で項目を絞る。状態は絞った項目だけで計算し直し、
    他のカードの異常を引きずらない。取得失敗は全カード共通で失敗のまま保つ。"""
    if result is not None and select:
        items = [i for i in result.items if select(i)]
        status = (result.status if result.status is Status.ERROR
                  else model.worst(i.status for i in items) if items else Status.OK)
        result = dataclasses.replace(result, items=items, status=status)
    h = f"<h3>{esc(title)}</h3>"
    if result is None:
        return Raw(f'<article class="card s-empty">{h}<p class="empty">{esc(label("empty.collecting"))}</p></article>')
    meta = [esc(label("meta.fetched_at", datetime=fmt_dt(result.fetched_at)))]
    if source:
        meta.append(esc(label("meta.source", name=source)))
    parts = [f'<header>{h}{badge(result.status)}</header>']
    if result.status is Status.ERROR:
        meta.append(esc(label("meta.last_success", datetime=fmt_dt(result.last_success_at)))
                    if result.last_success_at else "")
        parts.append(f'<p class="err" role="alert">{esc(label("meta.error_reason", reason=result.error or "-"))}</p>')
    parts.append(f'<p class="meta">{" / ".join(m for m in meta if m)}</p>')
    if result.items:
        body = render_body(result.items)
        parts.append(f'<div class="prev">{body}</div>' if result.status is Status.ERROR else str(body))
    elif result.status is not Status.ERROR:
        parts.append(f'<p class="empty">{esc(empty or label("empty.generic"))}</p>')
    return Raw(f'<article class="card s-{result.status.value}">{"".join(parts)}</article>')


def pick_range(query: Mapping[str, list[str]], param: str, options: Sequence[str]) -> str:
    """不正な値や省略は先頭の選択肢に倒す。"""
    v = (query.get(param) or [""])[0]
    return v if v in options else options[0]


def range_nav(anchor: str, param: str, options: Sequence[str], query: Mapping[str, list[str]]) -> Raw:
    cur = pick_range(query, param, options)
    links = []
    for o in options:
        q = urlencode([(k, v) for k, vs in query.items() if k != param for v in vs] + [(param, o)])
        cur_attr = ' aria-current="true" class="cur"' if o == cur else ""
        links.append(f'<a href="?{esc(q)}#sec-{esc(anchor)}"{cur_attr}>{esc(label("dash.range." + o))}</a>')
    return Raw(f'<p class="range">{"".join(links)}</p>')
