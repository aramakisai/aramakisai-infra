"""当日監視画面 (/admin/festival) の HTML と断片。保存済みの評価結果だけを読み、取得も評価もしない。"""
import json
from datetime import datetime, timedelta

import model
from festival.labels import label
from model import FestivalItem, Status
from render.components import JST, Raw, _MARK, esc

PAGE_PATH = "/admin/festival"
FRAGMENT_PATH = "/admin/festival/fragment"
TABS = ("visitor", "function", "capacity", "infra")
# 通常の表示 (タイル高 84px) が 1920x1080 の本文に収まる最大数 (6 列 x 9 行)。超えたら 64px の小型表示にする
DENSE_THRESHOLD = 54
EVAL_STALE_AFTER = timedelta(seconds=60)
BAND_NAMES_MAX = 5

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


def _tile(i: FestivalItem) -> str:
    s = _view_status(i)
    return (f'<button type="button" class="tile s-{s}" data-key="{esc(i.key)}" data-tab="{esc(i.category)}" '
            f'aria-controls="detail" aria-pressed="false"><span class="n">{esc(i.label)}</span>'
            f'<span class="st">{_badge(s)}</span><span class="v">{esc(i.value)}</span>'
            f'<span class="t">{_hms(i.fetched_at)}</span></button>')


def _detail(i: FestivalItem) -> str:
    s = _view_status(i)
    rows = []
    if i.threshold:
        rows.append((label("fest.detail.threshold"), esc(i.threshold)))
    if i.observed_at:
        rows.append((label("fest.detail.observed_at"), _hms(i.observed_at)))
    if i.fetched_at:
        rows.append((label("fest.detail.fetched_at"), _hms(i.fetched_at)))
    dl = "".join(f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in rows)
    note = f'<p class="note">{esc(i.note)}</p>' if i.note else ""
    href = _href(i.link)
    link = ""
    if href:
        ext = ' target="_blank" rel="noopener"' if href.startswith("https://") else ""
        link = f'<p><a class="btn" href="{esc(href)}"{ext}>{esc(label("fest.detail.link"))}</a></p>'
    return (f'<template data-detail-for="{esc(i.key)}"><h2>{esc(i.label)}</h2><p>{_badge(s)}</p>'
            f'<p class="val">{esc(i.value)}</p><dl>{dl}</dl>{note}{link}</template>')


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


def _panel(cat: str, items, selected: bool) -> str:
    dense = ' data-dense="true"' if len(items) > DENSE_THRESHOLD else ""
    body = "".join(_tile(i) + _detail(i) for i in items)
    return (f'<div role="tabpanel" class="panel" id="panel-{cat}" data-tab-id="{cat}" aria-labelledby="tab-{cat}"'
            f'{"" if selected else " hidden"}{dense}>{body}</div>')


def render_fragment(store, festival, now: datetime) -> str:
    """festival は config.festival。None (宣言なし) のときは宣言が無い旨だけを出す。"""
    state = store.load_festival_state()
    stale = "true" if _eval_stale(state, now) else "false"
    at = model.iso(state.evaluated_at) if state.evaluated_at else ""
    root = f'<div id="festival-fragment" data-evaluated-at="{at}" data-eval-stale="{stale}">'
    if festival is None:
        return f'{root}<p class="unconf">{esc(label("fest.unconfigured"))}</p></div>'
    items = store.load_festival_items()
    by = {c: [i for i in items if i.category == c] for c in TABS}
    tabs = "".join(_tab_head(c, by[c], c == TABS[0]) for c in TABS)
    panels = "".join(_panel(c, by[c], c == TABS[0]) for c in TABS)
    return (f'{root}{_band(items, state, now)}<nav class="tabs" role="tablist">{tabs}</nav>'
            f'<div class="panels">{panels}</div></div>')


CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--fg:#1c2330;--card:#fff;--line:#d5dae2;--mute:#5b6677;
--ok:#1d7a3a;--warn:#9a5b00;--crit:#b3261e;--error:#6a2c91;--stale:#4a5568;--empty:#4a5568;--info:#4a5568}
@media(prefers-color-scheme:dark){:root{--bg:#12161c;--fg:#e6e9ef;--card:#1b212b;--line:#343c4a;--mute:#9aa6b8;
--ok:#6fcf8a;--warn:#f0b04a;--crit:#ff8a80;--error:#d7a5f5;--stale:#a9b4c6;--empty:#a9b4c6;--info:#a9b4c6}}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,"Hiragino Sans","Noto Sans JP",sans-serif;
display:grid;grid-template-columns:minmax(0,1fr) 420px;grid-template-rows:auto 56px minmax(0,1fr);overflow:hidden}
#festival-fragment{display:contents}
.band{grid-column:1/-1;grid-row:1;max-height:120px;overflow:hidden;display:grid;grid-template-columns:1fr auto;
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
.panel{height:100%;overflow:auto;padding:12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
grid-auto-rows:84px;gap:8px;align-content:start}
.panel[hidden]{display:none}.panel[data-dense=true]{grid-auto-rows:64px}
.tile{display:grid;grid-template-columns:auto 1fr;grid-template-areas:"n n" "st v" "t t";align-content:center;
gap:2px 8px;padding:6px 10px;min-width:0;text-align:left;font:inherit;color:var(--fg);background:var(--card);
border:1px solid var(--line);border-left:6px solid var(--ok);border-radius:6px;cursor:pointer}
.panel[data-dense=true] .tile{grid-template-areas:"n t" "st v";padding:4px 10px}
.tile.s-warn{border-left-color:var(--warn)}.tile.s-crit{border-left-color:var(--crit);background:color-mix(in srgb,var(--crit) 10%,var(--card))}
.tile.s-error{border-left-color:var(--error)}.tile.s-stale,.tile.s-empty{border-left-color:var(--stale)}
.tile.s-info{border-left-color:var(--info);border-left-style:dashed}
.tile.sel{outline:3px solid var(--fg);outline-offset:-3px}
.tile .n{grid-area:n;font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .st{grid-area:st}.tile .v{grid-area:v;font-size:18px;font-weight:600;text-align:right;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}.tile .t{grid-area:t;color:var(--mute);font-size:13px}
.panel[data-dense=true] .tile .t{text-align:right}
#detail{grid-column:2;grid-row:3;overflow:auto;padding:16px;background:var(--card);border-left:1px solid var(--line)}
#detail h2{font-size:20px;margin:0 0 8px}#detail .val{font-size:26px;font-weight:700;margin:8px 0}
#detail dl{display:grid;grid-template-columns:auto 1fr;gap:4px 12px}#detail dt{color:var(--mute)}#detail dd{margin:0}
.btn{display:inline-block;padding:6px 12px;border:1px solid var(--line);border-radius:6px;color:var(--fg);text-decoration:none}
.gone{color:var(--mute)}.unconf{grid-column:1/-1;grid-row:1/-1;margin:auto;font-size:24px}
button:focus-visible,a:focus-visible{outline:3px solid var(--fg);outline-offset:2px}
"""

JS = r"""
(()=>{
const L=JSON.parse(document.getElementById('festival-labels').textContent);
const TABS=['visitor','function','capacity','infra'];
const $=(s,r=document)=>r.querySelector(s);
const hashTab=()=>{const h=location.hash.slice(1);return TABS.includes(h)?h:'visitor'};
const st={tab:hashTab(),key:null,last:Date.now(),reason:null};
const hms=ms=>new Date(ms).toLocaleTimeString('ja-JP',{timeZone:'Asia/Tokyo',hour12:false});
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
  root.querySelectorAll('.tile').forEach(t=>{const on=t.dataset.key===st.key;t.classList.toggle('sel',on);t.setAttribute('aria-pressed',on)});
  const d=$('#detail');
  if(st.key===null)d.replaceChildren();
  else{
    const tpl=[...root.querySelectorAll('template')].find(x=>x.dataset.detailFor===st.key);
    if(tpl)d.replaceChildren(tpl.content.cloneNode(true));
    else{const p=document.createElement('p');p.className='gone';p.textContent=L.gone;d.replaceChildren(p)}
  }
  warn();
}
async function refresh(){
  const ac=new AbortController(),to=setTimeout(()=>ac.abort(),10000);
  try{
    const r=await fetch(L.fragment,{credentials:'same-origin',cache:'no-store',redirect:'manual',signal:ac.signal});
    if(r.type==='opaqueredirect'||r.status===401)return{kind:'auth'};
    if(r.status===403)return{kind:'forbidden'};
    if(!r.ok)return{kind:'server',status:r.status};
    return{kind:'ok',html:await r.text()};
  }catch(e){return{kind:'network'}}finally{clearTimeout(to)}
}
function apply(res){
  if(res.kind==='ok'){
    const n=new DOMParser().parseFromString(res.html,'text/html').getElementById('festival-fragment');
    if(!n)return apply({kind:'server',status:200});
    const f=document.activeElement;
    const sel=f&&f.dataset&&(f.dataset.key?'.tile[data-key="'+CSS.escape(f.dataset.key)+'"]':f.id&&f.getAttribute('role')==='tab'?'#'+f.id:null);
    $('#festival-fragment').replaceWith(document.importNode(n,true));
    st.last=Date.now();st.reason=null;paint();
    if(sel){const e=$(sel);if(e)e.focus()}
    return;
  }
  st.reason=res.kind;paint();
}
function selectTab(t){st.tab=t;history.replaceState(null,'','#'+t);paint()}
document.addEventListener('click',e=>{
  const b=e.target.closest('[role=tab]');if(b){selectTab(b.dataset.tabId);return}
  const t=e.target.closest('.tile');if(t){st.key=t.dataset.key;paint()}
});
document.addEventListener('keydown',e=>{
  const b=e.target.closest&&e.target.closest('[role=tab]');if(!b)return;
  const i=TABS.indexOf(b.dataset.tabId);
  const j={ArrowRight:(i+1)%4,ArrowLeft:(i+3)%4,Home:0,End:3}[e.key];
  if(j===undefined)return;
  e.preventDefault();selectTab(TABS[j]);$('#tab-'+TABS[j]).focus();
});
window.addEventListener('hashchange',()=>{st.tab=hashTab();paint()});
setInterval(()=>refresh().then(apply),15000);
setInterval(warn,5000);
paint();
})();
"""


def render_page(store, festival, now: datetime) -> str:
    labels = {"gone": label("fest.detail.gone"), "stale": label("fest.band.stale", time="{time}"),
              "reauth": label("fest.band.reauth"), "page": PAGE_PATH, "fragment": FRAGMENT_PATH}
    # </script> 等で JSON ブロックが閉じないよう < をエスケープする
    data = json.dumps(labels, ensure_ascii=False).replace("<", "\\u003c")
    return ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(label("fest.title"))}</title><style>{CSS}</style></head><body>'
            f'{render_fragment(store, festival, now)}<aside id="detail" aria-live="polite"></aside>'
            f'<script type="application/json" id="festival-labels">{data}</script><script>{JS}</script></body></html>')
