from datetime import datetime, timezone
from typing import Mapping

from model import SourceResult, Status, safe_error
from render.components import badge, esc, fmt_dt
from render.labels import label
from render.sections import Section, load_sections

CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1c2330;--card:#fff;--line:#d5dae2;--mute:#5b6677;
--ok:#1d7a3a;--warn:#9a5b00;--crit:#b3261e;--error:#6a2c91;--stale:#4a5568;--empty:#4a5568}
@media(prefers-color-scheme:dark){:root{--bg:#12161c;--fg:#e6e9ef;--card:#1b212b;--line:#343c4a;--mute:#9aa6b8;
--ok:#6fcf8a;--warn:#f0b04a;--crit:#ff8a80;--error:#d7a5f5;--stale:#a9b4c6;--empty:#a9b4c6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Hiragino Sans","Noto Sans JP",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px 48px}
.top{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between;padding:16px 0}
.top h1{font-size:1.4rem;margin:0}.top .acts{display:flex;gap:8px;flex-wrap:wrap}
.btn{display:inline-block;padding:6px 12px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);text-decoration:none}
nav.sections{display:flex;flex-wrap:wrap;gap:4px 12px;padding:8px 0;border-bottom:1px solid var(--line);margin-bottom:16px}
nav.sections a{color:var(--fg)}
section.sec{margin:24px 0}section.sec>h2{font-size:1.2rem;border-left:4px solid var(--line);padding-left:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:12px 0}
.card header{display:flex;gap:8px;align-items:center;justify-content:space-between}.card h3{margin:0;font-size:1rem}
.card.s-error{border:2px solid var(--error)}.card.s-crit{border:2px solid var(--crit)}.card.s-warn{border-color:var(--warn)}
.meta{color:var(--mute);font-size:.85rem;margin:4px 0}.err{color:var(--error);font-weight:600;margin:6px 0}
.prev{opacity:.5}.empty{color:var(--mute)}.note{font-size:.85rem}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid currentColor;font-size:.8rem;font-weight:600;white-space:nowrap}
.s-ok .badge,.badge.s-ok{color:var(--ok)}.badge.s-warn{color:var(--warn)}.badge.s-crit{color:var(--crit)}
.badge.s-error{color:var(--error);background:color-mix(in srgb,var(--error) 12%,transparent)}
.badge.s-stale{color:var(--stale)}.badge.s-empty{color:var(--empty)}
.tablewrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line)}td.num{text-align:right}
.summary{border:1px solid var(--line);border-radius:8px;background:var(--card);padding:8px 16px}
.summary ul{margin:4px 0;padding-left:20px}.summary h2{font-size:1rem;margin:4px 0}
.range{display:flex;gap:8px;margin:4px 0}.range a{padding:2px 10px;border:1px solid var(--line);border-radius:12px;color:var(--fg);text-decoration:none}
.range a.cur{background:var(--fg);color:var(--bg)}
figure.chart{margin:12px 0}figcaption{font-weight:600}.svg{width:100%;height:auto;max-width:640px}
.svg text{fill:currentColor;font-size:11px}.svg .grid{stroke:var(--line)}.svg .axt{fill:var(--mute)}
.legend{list-style:none;display:flex;flex-wrap:wrap;gap:4px 14px;padding:0;margin:4px 0;font-size:.85rem}
.sw{display:inline-block;width:10px;height:10px;margin-right:4px}
details.numbers summary{cursor:pointer;color:var(--mute)}details.numbers[open] .show,details.numbers:not([open]) .hide{display:none}
"""


def _flagged(snapshot: Mapping[str, SourceResult]):
    """要確認の項目: (source_id, 状態, 表示文) の一覧。取得失敗の情報源は直前の値を数えない。"""
    out = []
    for sid, r in sorted(snapshot.items()):
        if r.status is Status.ERROR:
            out.append((sid, Status.ERROR, r.error or ""))
            continue
        hits = [i for i in r.items if i.status in (Status.CRIT, Status.WARN)]
        for i in hits:
            out.append((sid, i.status, " ".join(x for x in (i.label, i.note) if x)))
        if not hits and r.status in (Status.CRIT, Status.WARN):
            out.append((sid, r.status, ""))
    return out


def _summary(snapshot, sections: list[Section]) -> str:
    flagged = _flagged(snapshot)
    where = {sid: s for s in sections for sid in s.source_ids}
    n = {st: sum(1 for f in flagged if f[1] is st) for st in (Status.CRIT, Status.WARN, Status.ERROR)}
    head = f'<h2>{esc(label("dash.summary.title"))}</h2>'
    if not flagged:
        return f'<div class="summary">{head}<p>{esc(label("dash.summary.none"))}</p></div>'
    counts = label("dash.summary.counts", crit=n[Status.CRIT], warn=n[Status.WARN], error=n[Status.ERROR])
    lis = []
    for sid, st, text in flagged:
        s = where.get(sid)
        name = esc(label(s.title)) if s else esc(sid)
        if s:
            name = f'<a href="#sec-{esc(s.anchor)}">{name}</a>'
        lis.append(f"<li>{badge(st)} {name} <code>{esc(sid)}</code> {esc(text)}</li>")
    return f'<div class="summary">{head}<p>{esc(counts)}</p><ul>{"".join(lis)}</ul></div>'


def _section(s: Section, snapshot, query) -> str:
    try:
        body = s.render(snapshot, query)
    except Exception as e:
        # 1 つの節の描画不具合でページ全体を落とさない。
        body = f'<p class="err" role="alert">{badge(Status.ERROR)} {esc(safe_error(e))}</p>'
    return f'<section class="sec" id="sec-{esc(s.anchor)}"><h2>{esc(label(s.title))}</h2>{body}</section>'


def render(snapshot: Mapping[str, SourceResult], query: Mapping[str, list[str]],
           now: datetime | None = None, sections: list[Section] | None = None) -> str:
    sections = load_sections() if sections is None else sections
    fetched = [r.fetched_at for r in snapshot.values()]
    updated = max(fetched) if fetched else now or datetime.now(timezone.utc)
    nav = "".join(f'<a href="#sec-{esc(s.anchor)}">{esc(label(s.nav))}</a>' for s in sections)
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{esc(label("dash.title"))}</title><style>{CSS}</style></head><body><div class="wrap">'
        f'<div class="top"><div><h1>{esc(label("dash.title"))}</h1>'
        f'<div class="meta">{esc(label("dash.updated", datetime=fmt_dt(updated)))}</div></div>'
        f'<div class="acts"><a class="btn" href="">{esc(label("dash.button.reload"))}</a>'
        f'<a class="btn" href="/">{esc(label("dash.button.portal"))}</a>'
        f'<a class="btn" href="/oauth2/sign_out">{esc(label("dash.button.logout"))}</a></div></div>'
        f'{_summary(snapshot, sections)}<nav class="sections">{nav}</nav>'
        + "".join(_section(s, snapshot, query) for s in sections)
        + "</div></body></html>")
