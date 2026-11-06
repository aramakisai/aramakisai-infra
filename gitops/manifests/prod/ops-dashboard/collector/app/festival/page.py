"""当日監視画面 (/admin/festival) の HTML と断片。保存済みの評価結果だけを読み、取得も評価もしない。"""
import json
import math
from datetime import datetime, timedelta
from typing import NamedTuple

import model
from festival.chart import day_chart, sparkline
from festival.labels import label
from model import FestivalItem, Status
from render.components import JST, Raw, _MARK, esc

PAGE_PATH = "/admin/festival"
FRAGMENT_PATH = "/admin/festival/fragment"
TABS = ("visitor", "function", "capacity", "infra")
# 格子の領域 (1360x904 から内側の余白 12px を引いた寸法) とタイルの寸法の制約。ブラウザの幅には依存させない
GRID_W, GRID_H, GAP = 1336, 880, 8
TILE_MIN_W, TILE_MIN_H, TILE_MAX_W, TILE_MAX_H = 200, 108, 640, 360
STANDARD_MIN_W, STANDARD_MIN_H = 240, 198
MAX_COLS = 6
SPARK_MINUTES = 60
EVAL_STALE_AFTER = timedelta(seconds=60)
BAND_NAMES_MAX = 5
KEY_MAX = 200
BREAKDOWN_MAX_ROWS = 11

_MARKS = {**{s.value: m for s, m in _MARK.items()}, "info": "◇"}


def _view_status(i: FestivalItem) -> str:
    """表示のみの項目は、取得失敗・取得遅延でない限り正常の色を使わない。"""
    return "info" if not i.judged and i.status is Status.OK else i.status.value


def _badge(name: str) -> str:
    return f'<span class="badge s-{name}"><span aria-hidden="true">{_MARKS[name]}</span> {esc(label("fest.status." + name))}</span>'


def _hms(dt: datetime | None) -> str:
    return dt.astimezone(JST).strftime("%H:%M:%S") if dt else "-"


def _href(link: str | None) -> str | None:
    if link and (link.startswith("https://") or (link.startswith("/") and not link.startswith("//"))):
        return link
    return None


class Grid(NamedTuple):
    cols: int
    rows: int
    w: int
    h: int
    size: str


def tile_grid(n: int) -> Grid | None:
    """タブの項目数からタイルの列数・行数・寸法を決める。0 個は None。"""
    if n <= 0:
        return None
    best = None
    for c in range(1, MAX_COLS + 1):
        r = math.ceil(n / c)
        w = min((GRID_W - (c - 1) * GAP) // c, TILE_MAX_W)
        h = min((GRID_H - (r - 1) * GAP) // r, TILE_MAX_H)
        if w < TILE_MIN_W or h < TILE_MIN_H:
            continue
        if best is None or w * h > best[2] * best[3]:
            best = (c, r, w, h)
    if best is None:
        # 42 個を超えた場合。設計に定めが無いため、最小寸法の 6 列で置いて格子の領域のスクロールに任せる
        c = MAX_COLS
        best = (c, math.ceil(n / c), (GRID_W - (c - 1) * GAP) // c, TILE_MIN_H)
    c, r, w, h = best
    return Grid(c, r, w, h, "standard" if w >= STANDARD_MIN_W and h >= STANDARD_MIN_H else "compact")


def _times(i: FestivalItem) -> str:
    fetched = label("fest.tile.fetched_at", time=_hms(i.fetched_at))
    if not i.observed_at:
        return fetched
    return f'{label("fest.tile.observed_at", time=_hms(i.observed_at))}・{fetched}'


def _tile(i: FestivalItem, spark: str) -> str:
    s = _view_status(i)
    chart = i.unit is not None
    # 自動選択がブラウザ側で model.worst と同じ順序を使えるよう、判定対象の項目の重さを渡す
    rank = f' data-rank="{model._SEVERITY[i.status]}"' if i.judged else ""
    sp = f'<span class="sp">{spark}</span>' if chart else ""
    return (f'<button type="button" class="tile s-{s}{"" if chart else " nc"}" data-key="{esc(i.key)}"{rank} '
            f'data-tab="{esc(i.category)}" aria-controls="detail" aria-pressed="false">'
            f'<span class="h"><span class="n">{esc(i.label)}</span>{_badge(s)}</span>'
            f'<span class="v">{esc(i.value)}</span>{sp}'
            f'<span class="th">{esc(i.threshold or "")}</span><span class="no">{esc(i.note or "")}</span>'
            f'<span class="t">{esc(_times(i))}</span></button>')


def _cell(v: str) -> str:
    return esc(label(v) if v.startswith("fest.bool.") else v)


def _breakdown(bd) -> str:
    rows = bd.rows
    more = ""
    if len(rows) > BREAKDOWN_MAX_ROWS:
        more = (f'<tr class="more"><td colspan="{len(bd.columns)}">'
                f'{esc(label("fest.detail.more_rows", n=len(rows) - (BREAKDOWN_MAX_ROWS - 1)))}</td></tr>')
        rows = rows[:BREAKDOWN_MAX_ROWS - 1]
    head = "".join(f"<th>{esc(label(c))}</th>" for c in bd.columns)
    body = "".join("<tr>" + "".join(f"<td>{_cell(v)}</td>" for v in r) + "</tr>" for r in rows)
    return (f'<section class="db"><h3>{esc(label(bd.title))}</h3>'
            f'<table><thead><tr>{head}</tr></thead><tbody>{body}{more}</tbody></table></section>')


def _chart(store, i: FestivalItem, state, now: datetime) -> str:
    end = (state.evaluated_at or now).astimezone(JST)
    start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    points = store.load_festival_history(start, end, key=i.key).get(i.key, [])
    return (f'<section class="dc"><h3>{esc(label("fest.detail.chart", start=start.strftime("%H:%M"), end=end.strftime("%H:%M")))}</h3>'
            f'{day_chart(points, start, end, i.unit, i.warn, i.crit, i.label)}'
            f'<p class="gap">{esc(label("fest.detail.chart_gap"))}</p></section>')


def _detail(store, i: FestivalItem, state, now: datetime) -> str:
    s = _view_status(i)
    rows = []
    if i.threshold:
        rows.append((label("fest.detail.threshold"), i.threshold))
    if i.observed_at:
        rows.append((label("fest.detail.observed_at"), _hms(i.observed_at)))
    if i.fetched_at:
        rows.append((label("fest.detail.fetched_at"), _hms(i.fetched_at)))
    if i.note:
        rows.append((label("fest.detail.note"), i.note))
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    href = _href(i.link)
    link = ""
    if href and i.link_label:
        ext = ' target="_blank" rel="noopener"' if href.startswith("https://") else ""
        link = (f'<p class="dl"><a class="dlink" href="{esc(href)}"{ext}>'
                f'{esc(label("fest.detail.link", name=label(i.link_label)))}</a></p>')
    return (f'<div class="dh"><h2>{esc(i.label)}</h2>{_badge(s)}</div><p class="dv">{esc(i.value)}</p>'
            f'<dl class="di">{dl}</dl>{_chart(store, i, state, now) if i.unit else ""}'
            f'{_breakdown(i.breakdown) if i.breakdown else ""}{link}')


def _detail_aside(store, items, state, key: str | None, now: datetime) -> str:
    if not key:
        body = f'<p class="gone">{esc(label("fest.detail.empty"))}</p>'
    else:
        i = next((x for x in items if x.key == key), None) if len(key) <= KEY_MAX else None
        body = _detail(store, i, state, now) if i else f'<p class="gone">{esc(label("fest.detail.gone"))}</p>'
    return f'<aside id="detail" aria-live="polite">{body}</aside>'


def _band(items, state, now: datetime) -> str:
    judged = [i for i in items if i.judged]
    crit = [i for i in judged if i.status is Status.CRIT]
    warn = [i for i in judged if i.status is Status.WARN]
    shown = "".join(f"<li>{esc(i.label)}</li>" for i in crit[:BAND_NAMES_MAX])
    if len(crit) > BAND_NAMES_MAX:
        shown += f'<li>{esc(label("fest.band.more", n=len(crit) - BAND_NAMES_MAX))}</li>'
    stopped = ""
    if _eval_stale(state, now):
        stopped = (f'<span class="evalstop" role="alert">{esc(label("fest.band.eval_stopped", time=_hms(state.evaluated_at)))}'
                   "</span>")
    cls = "s-crit" if crit else ("s-warn" if warn else "s-ok")
    return (f'<header id="festival-band" class="band {cls}"><span class="counts">'
            f'{esc(label("fest.band.counts", crit=len(crit), warn=len(warn)))}</span>'
            f'<span class="upd">{esc(label("fest.band.updated", time=_hms(state.evaluated_at)))}</span>'
            f'<ul class="names">{shown}</ul>{stopped}'
            '<span id="festival-warn" class="warn" role="status" hidden></span></header>')


def _eval_stale(state, now: datetime) -> bool:
    return state.evaluated_at is None or now - state.evaluated_at > EVAL_STALE_AFTER


def _tab_head(cat: str, items, selected: bool) -> str:
    judged = [i for i in items if i.judged]
    n_crit = sum(1 for i in judged if i.status is Status.CRIT)
    worst = model.worst(i.status for i in judged) if judged else None
    head = _badge(worst.value) if worst else '<span class="badge s-empty"><span aria-hidden="true">–</span></span>'
    return (f'<button type="button" role="tab" id="tab-{cat}" data-tab-id="{cat}" aria-controls="panel-{cat}" '
            f'aria-selected="{str(selected).lower()}" tabindex="{0 if selected else -1}" '
            f'class="tab s-{worst.value if worst else "empty"}"><span class="tn">{esc(label("fest.tab." + cat))}</span>'
            f'{head}<span class="cnt">{esc(label("fest.status.crit"))} {n_crit} 件</span></button>')


def _panel(cat: str, items, selected: bool, sparks: dict[str, str]) -> str:
    g = tile_grid(len(items))
    grid = (f' data-size="{g.size}" style="--cols:{g.cols};--tile-w:{g.w}px;--tile-h:{g.h}px"') if g else ""
    body = "".join(_tile(i, sparks.get(i.key, "")) for i in items)
    return (f'<div role="tabpanel" class="panel" id="panel-{cat}" data-tab-id="{cat}" aria-labelledby="tab-{cat}"'
            f'{"" if selected else " hidden"}{grid}>{body}</div>')


def _sparks(store, items, state) -> dict[str, str]:
    """グラフの対象の項目の直近 60 分を、全項目まとめて 1 回で読んで描く。"""
    if state.evaluated_at is None or not any(i.unit for i in items):
        return {}
    end = state.evaluated_at.replace(second=0, microsecond=0)
    hist = store.load_festival_history(end - timedelta(minutes=SPARK_MINUTES - 1), end)
    return {i.key: sparkline(hist[i.key], end) for i in items if i.unit and i.key in hist}


def render_fragment(store, festival, now: datetime, key: str | None = None) -> str:
    """festival は config.festival。None (宣言なし) のときは宣言が無い旨だけを出す。key は選択中の項目。"""
    state = store.load_festival_state()
    stale = "true" if _eval_stale(state, now) else "false"
    at = model.iso(state.evaluated_at) if state.evaluated_at else ""
    root = f'<div id="festival-fragment" data-evaluated-at="{at}" data-eval-stale="{stale}" data-key="{esc(key if key and len(key) <= KEY_MAX else "")}">'
    if festival is None:
        return f'{root}<p class="unconf">{esc(label("fest.unconfigured"))}</p></div>'
    items = store.load_festival_items()
    by = {c: [i for i in items if i.category == c] for c in TABS}
    tabs = "".join(_tab_head(c, by[c], c == TABS[0]) for c in TABS)
    sparks = _sparks(store, items, state)
    panels = "".join(_panel(c, by[c], c == TABS[0], sparks) for c in TABS)
    return (f'{root}{_band(items, state, now)}<nav class="tabs" role="tablist">{tabs}</nav>'
            f'<div class="panels">{panels}</div>{_detail_aside(store, items, state, key, now)}</div>')


CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1c2330;--card:#fff;--line:#d5dae2;--mute:#5b6677;
--ok:#1d7a3a;--warn:#9a5b00;--crit:#b3261e;--error:#6a2c91;--stale:#4a5568;--empty:#4a5568;--info:#4a5568}
@media(prefers-color-scheme:dark){:root{--bg:#12161c;--fg:#e6e9ef;--card:#1b212b;--line:#343c4a;--mute:#9aa6b8;
--ok:#6fcf8a;--warn:#f0b04a;--crit:#ff8a80;--error:#d7a5f5;--stale:#a9b4c6;--empty:#a9b4c6;--info:#a9b4c6}}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;
display:grid;grid-template-columns:1360px 560px;grid-template-rows:120px 56px 904px;overflow:hidden}
#festival-fragment{display:contents}
.band{grid-column:1/-1;grid-row:1;height:120px;overflow:hidden;display:grid;grid-template-columns:1fr auto;
gap:2px 24px;align-items:center;padding:8px 16px;background:var(--card);border-bottom:4px solid var(--ok)}
.band.s-warn{border-bottom-color:var(--warn)}.band.s-crit{border-bottom-color:var(--crit)}
.counts{font-size:26px;font-weight:700}.upd{color:var(--mute);font-size:18px}
.names{grid-column:1/-1;list-style:none;display:flex;flex-wrap:nowrap;gap:0 16px;margin:0;padding:0;overflow:hidden;
white-space:nowrap;color:var(--crit);font-weight:600;font-size:18px;min-height:25px}
.warn,.evalstop{grid-column:1/-1;font-weight:600;color:var(--warn)}.warn.strong,.evalstop{color:var(--crit)}
.warn a{color:inherit}
.tabs{grid-column:1/-1;grid-row:2;display:flex;gap:4px;padding:0 12px;align-items:stretch;border-bottom:1px solid var(--line)}
.tab{flex:1;display:flex;align-items:center;justify-content:center;gap:12px;border:1px solid var(--line);border-bottom:0;
background:var(--bg);color:var(--fg);font:inherit;font-size:17px;cursor:pointer;border-radius:8px 8px 0 0}
.tab[aria-selected=true]{background:var(--card);font-weight:700;box-shadow:inset 0 3px 0 var(--fg)}
.tab .cnt{color:var(--mute);font-size:14px}
.badge{display:inline-block;padding:0 8px;border-radius:10px;border:1px solid currentColor;font-size:13px;font-weight:600;white-space:nowrap}
.badge.s-ok{color:var(--ok)}.badge.s-warn{color:var(--warn)}.badge.s-crit{color:var(--crit)}
.badge.s-error{color:var(--error);background:color-mix(in srgb,var(--error) 12%,transparent)}
.badge.s-stale,.badge.s-info{color:var(--stale)}.badge.s-empty{color:var(--empty)}
.panels{grid-column:1;grid-row:3;min-height:0}
.panel{height:100%;overflow:auto;padding:12px;display:grid;grid-template-columns:repeat(var(--cols),var(--tile-w));
grid-auto-rows:var(--tile-h);gap:8px;align-content:start;justify-content:start}
.panel[hidden]{display:none}
.tile{display:grid;grid-template-columns:minmax(0,1fr);min-width:0;min-height:0;overflow:hidden;text-align:left;
font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-left:6px solid var(--ok);
border-radius:6px;cursor:pointer;padding:10px 12px;gap:4px;
grid-template-areas:"h" "v" "sp" "th" "no" "t";
grid-template-rows:24px 38px max(40px,calc(var(--tile-h) - 158px)) 18px 18px 18px}
.tile.s-warn{border-left-color:var(--warn)}.tile.s-crit{border-left-color:var(--crit);background:color-mix(in srgb,var(--crit) 10%,var(--card))}
.tile.s-error{border-left-color:var(--error)}.tile.s-stale,.tile.s-empty{border-left-color:var(--stale)}
.tile.s-info{border-left-color:var(--info);border-left-style:dashed}
.tile.sel{outline:3px solid var(--fg);outline-offset:-3px}
.tile.s-ok{color:var(--fg)}
.tile .h{grid-area:h;display:flex;align-items:center;gap:8px;min-width:0}
.tile .n{flex:1;min-width:0;font-weight:700;font-size:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .v{grid-area:v;font-size:30px;font-weight:700;line-height:38px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .sp{grid-area:sp;min-height:0;overflow:hidden;color:var(--ok)}
.tile.s-warn .sp{color:var(--warn)}.tile.s-crit .sp{color:var(--crit)}.tile.s-error .sp{color:var(--error)}
.tile.s-stale .sp,.tile.s-empty .sp,.tile.s-info .sp{color:var(--stale)}
.tile .spark{display:block;width:100%;height:100%}
.panel[data-size=standard] .tile.nc{grid-template-areas:"h" "v" "th" "no" "t";
grid-template-rows:24px calc(42px + max(40px,calc(var(--tile-h) - 158px))) 18px 18px 18px}
/* 30px に収まらない現在値の続きを、同じ要素の 2 行目以降に 15px で折り返して出す */
.panel[data-size=standard] .tile.nc .v{font-size:15px;font-weight:400;line-height:normal;white-space:normal;
overflow:hidden;overflow-wrap:anywhere;text-overflow:clip}
.panel[data-size=standard] .tile.nc .v::first-line{font-size:30px;font-weight:700;line-height:38px}
.tile .th,.tile .no,.tile .t{font-size:13px;line-height:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .th{grid-area:th}.tile .no{grid-area:no}.tile .t{grid-area:t;color:var(--mute)}
.panel[data-size=compact] .tile{padding:4px 8px;gap:2px;grid-template-columns:55fr 45fr;
grid-template-areas:"h h" "v sp" "th th" "no no" "t t";grid-template-rows:20px max(28px,calc(var(--tile-h) - 80px)) 14px 14px 14px}
.panel[data-size=compact] .tile.nc{grid-template-areas:"h h" "v v" "th th" "no no" "t t"}
.panel[data-size=compact] .tile.nc .sp{display:none}
.panel[data-size=compact] .tile .n{font-size:14px}.panel[data-size=compact] .tile .badge{font-size:12px}
.panel[data-size=compact] .tile .v{font-size:20px;line-height:28px;align-self:center}
.panel[data-size=compact] .tile .th,.panel[data-size=compact] .tile .no,.panel[data-size=compact] .tile .t{font-size:12px;line-height:14px}
#detail{grid-column:2;grid-row:3;height:904px;overflow:auto;padding:16px;background:var(--card);border-left:1px solid var(--line)}
#detail>*{margin:0}#detail>*+*{margin-top:16px}#detail>.dv,#detail>.di{margin-top:8px}
#detail .dh{height:28px;display:flex;align-items:center;gap:8px}
#detail h2{font-size:20px;line-height:28px;margin:0;min-width:0;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
#detail .dv{font-size:26px;font-weight:700;line-height:36px;height:36px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#detail dl{display:grid;grid-template-columns:auto 1fr;gap:0 12px;max-height:110px;overflow:hidden;font-size:15px;line-height:22px}
#detail dt{color:var(--mute)}
#detail dd{margin:0;min-width:0;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden}
#detail h3{margin:0;font-size:13px;line-height:20px;font-weight:400;color:var(--mute)}
#detail .dc{height:258px;overflow:hidden}
#detail .daychart{display:block;width:528px;height:220px;color:var(--mute);fill:currentColor}
#detail .daychart text{fill:currentColor}
#detail .chart-empty{height:220px;display:flex;align-items:center;justify-content:center;color:var(--mute)}
#detail .gap{margin:0;font-size:13px;line-height:18px;color:var(--mute)}
#detail .db{max-height:334px;overflow:hidden}
#detail .db h3{font-size:15px;line-height:20px;font-weight:700;color:var(--fg)}
#detail table{width:100%;table-layout:fixed;border-collapse:separate;border-spacing:0;font-size:13px}
#detail th,#detail td{box-sizing:border-box;padding:0 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-align:left}
#detail th{height:28px;line-height:27px;color:var(--mute);font-weight:600;border-bottom:1px solid var(--line)}
#detail td{height:26px;line-height:25px;border-bottom:1px solid var(--line)}
#detail tr.more td{color:var(--mute)}
#detail .dl{height:24px;font-size:13px;line-height:24px}
#detail .dlink{color:var(--fg);text-decoration:underline}
.gone{color:var(--mute)}.unconf{grid-column:1/-1;grid-row:1/-1;margin:auto;font-size:24px}
button:focus-visible,a:focus-visible{outline:3px solid var(--fg);outline-offset:2px}
"""

JS = r"""
(()=>{
const L=JSON.parse(document.getElementById('festival-labels').textContent);
const TABS=['visitor','function','capacity','infra'];
const $=(s,r=document)=>r.querySelector(s);
const hashTab=()=>{const h=location.hash.slice(1);return TABS.includes(h)?h:'visitor'};
// key: 利用者が選んだ項目、auto: 自動選択の項目、gone: key の項目が消えたことを 1 回表示済み、seq: 最後に出した断片の要求の連番
const st={tab:hashTab(),key:null,auto:null,gone:false,seq:0,last:Date.now(),reason:null};
const curKey=()=>st.key??st.auto;
const hms=ms=>new Date(ms).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
function warn(){
  const w=$('#festival-warn');if(!w)return;
  if(!st.reason){w.hidden=true;return}
  w.hidden=false;w.replaceChildren();
  if(st.reason==='auth'){
    w.append(L.reauth+' ');const a=document.createElement('a');a.href=L.page;a.textContent=L.page;w.append(a);
  }else w.textContent=L.stale.replace('{time}',hms(st.last));
  w.classList.toggle('strong',Date.now()-st.last>45000);
}
function paint(){
  const root=$('#festival-fragment');
  root.querySelectorAll('[role=tab]').forEach(b=>{const on=b.dataset.tabId===st.tab;b.setAttribute('aria-selected',on);b.tabIndex=on?0:-1});
  root.querySelectorAll('[role=tabpanel]').forEach(p=>{p.hidden=p.dataset.tabId!==st.tab});
  root.querySelectorAll('.tile').forEach(t=>{const on=t.dataset.key===curKey();t.classList.toggle('sel',on);t.setAttribute('aria-pressed',on)});
  warn();
}
async function refresh(key){
  const ac=new AbortController(),to=setTimeout(()=>ac.abort(),10000);
  try{
    const r=await fetch(key?L.fragment+'?key='+encodeURIComponent(key):L.fragment,{credentials:'same-origin',cache:'no-store',redirect:'manual',signal:ac.signal});
    if(r.type==='opaqueredirect'||r.status===401)return{kind:'auth'};
    if(r.status===403)return{kind:'forbidden'};
    if(!r.ok)return{kind:'server',status:r.status};
    return{kind:'ok',html:await r.text()};
  }catch(e){return{kind:'network'}}finally{clearTimeout(to)}
}
function load(loading){
  const seq=++st.seq;
  if(loading){const d=$('#detail'),p=document.createElement('p');p.className='gone';p.textContent=L.loading;d.replaceChildren(p)}
  refresh(curKey()).then(r=>apply(r,seq));
}
function pick(){
  const ts=[...document.querySelectorAll('#panel-'+st.tab+' .tile')];
  let b=null;
  for(const t of ts)if(t.dataset.rank&&(!b||+t.dataset.rank>+b.dataset.rank))b=t;
  return (b||ts[0])?.dataset.key??null;
}
function autoSelect(){
  if(st.key)return;
  st.auto=pick();paint();
  if(st.auto!==($('#festival-fragment').dataset.key||null))load(true);
}
function selectItem(key){
  st.key=key;st.gone=false;
  if(key===null)st.auto=pick();
  paint();load(true);
}
function apply(res,seq){
  if(seq!==st.seq)return;
  if(res.kind==='ok'){
    const n=new DOMParser().parseFromString(res.html,'text/html').getElementById('festival-fragment');
    if(!n)return apply({kind:'server',status:200},seq);
    const f=document.activeElement;
    const sel=f&&f.dataset&&(f.dataset.key?'.tile[data-key="'+CSS.escape(f.dataset.key)+'"]':f.id&&f.getAttribute('role')==='tab'?'#'+f.id:null);
    $('#festival-fragment').replaceWith(document.importNode(n,true));
    st.last=Date.now();st.reason=null;
    if(st.key){
      if(!$('.tile[data-key="'+CSS.escape(st.key)+'"]')){if(st.gone){st.key=null;st.gone=false}else st.gone=true}
      else st.gone=false;
    }
    paint();
    if(sel){const e=$(sel);if(e)e.focus()}
    autoSelect();
    return;
  }
  st.reason=res.kind;paint();
}
function selectTab(t){st.tab=t;history.replaceState(null,'','#'+t);paint();autoSelect()}
document.addEventListener('click',e=>{
  const b=e.target.closest('[role=tab]');if(b){selectTab(b.dataset.tabId);return}
  const t=e.target.closest('.tile');if(t)selectItem(t.dataset.key===st.key?null:t.dataset.key)
});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&st.key){selectItem(null);return}
  const b=e.target.closest&&e.target.closest('[role=tab]');if(!b)return;
  const i=TABS.indexOf(b.dataset.tabId);
  const j={ArrowRight:(i+1)%4,ArrowLeft:(i+3)%4,Home:0,End:3}[e.key];
  if(j===undefined)return;
  e.preventDefault();selectTab(TABS[j]);$('#tab-'+TABS[j]).focus();
});
window.addEventListener('hashchange',()=>{st.tab=hashTab();paint();autoSelect()});
setInterval(()=>load(false),15000);
setInterval(warn,5000);
paint();autoSelect();
})();
"""


def render_page(store, festival, now: datetime) -> str:
    labels = {"stale": label("fest.band.stale", time="{time}"),
              "reauth": label("fest.band.reauth"), "page": PAGE_PATH, "fragment": FRAGMENT_PATH,
              "loading": label("fest.detail.loading")}
    # </script> 等で JSON ブロックが閉じないよう < をエスケープする
    data = json.dumps(labels, ensure_ascii=False).replace("<", "\\u003c")
    return ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(label("fest.title"))}</title><style>{CSS}</style></head><body>'
            f'{render_fragment(store, festival, now)}'
            f'<script type="application/json" id="festival-labels">{data}</script><script>{JS}</script></body></html>')
