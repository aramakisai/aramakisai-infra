"""当日監視画面の実ブラウザ E2E (手動実行)。collector/app で `python3 -I tests/e2e_festival.py`。

CI の unittest discover の対象外 (ファイル名が test_ で始まらない)。ローカルの collector と一時 SQLite を使い、
Playwright の headless chromium を 1920x1080 で動かす。自動更新は Playwright の clock で進める。
"""
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

import main  # noqa: E402
import store as store_mod  # noqa: E402
from helpers import make_ctx  # noqa: E402
from festival.labels import label  # noqa: E402
from festival.page import TABS, tile_grid  # noqa: E402
from model import Breakdown, FestivalItem, Status  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = Path(os.environ.get("E2E_SHOTS", tempfile.gettempdir()))
CHROMIUM = next(iter(sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))[-1:]), None)


def item(key, status=Status.OK, category="visitor", value="1.0%", **kw):
    now = datetime.now(timezone.utc)
    base = dict(key=key, category=category, label=f"項目 {key}", status=status, value=value, threshold="異常 ≥ 5%",
                observed_at=now, fetched_at=now, source_ids=("festival.cloudflare",), link=None, note=None,
                notify=True, judged=True)
    return FestivalItem(**{**base, **kw})


BROWSER = []
_PW = []


def setUpModule():
    # Playwright の同期 API は 1 プロセスに 1 つだけ起動できる
    _PW.append(sync_playwright().start())
    BROWSER.append(_PW[0].chromium.launch(headless=True, executable_path=str(CHROMIUM) if CHROMIUM else None))


def tearDownModule():
    for close in (BROWSER[0].close, _PW[0].stop):
        try:
            close()
        except Exception:
            pass


class Base(unittest.TestCase):
    items: list = []

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = store_mod.Store(os.path.join(cls.tmp.name, "e2e.db"))
        cls.put(cls.items)
        cls.web = main.make_web_server(main.Collector(make_ctx(store=cls.store), cls.store, []), ("127.0.0.1", 0))
        threading.Thread(target=cls.web.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.web.server_address[1]}/admin/festival"
        cls.browser = BROWSER[0]

    @classmethod
    def tearDownClass(cls):
        for close in (cls.web.shutdown, cls.web.server_close, cls.store.close, cls.tmp.cleanup):
            try:
                close()
            except Exception:
                pass

    @classmethod
    def put(cls, items, history=None):
        cls.store.replace_festival_items(items, datetime.now(timezone.utc))
        if history:
            cls.store.write(lambda c: c.executemany(
                "INSERT OR REPLACE INTO festival_history(key,minute,value) VALUES (?,?,?)", history))

    def setUp(self):
        self.ctx = self.browser.new_context(viewport={"width": 1920, "height": 1080})
        self.page = self.ctx.new_page()
        self.page.clock.install()

    def tearDown(self):
        self.shot()
        self.ctx.close()

    def shot(self):
        self.page.screenshot(path=str(SHOTS / f"festival_{self.id().split('.')[-1]}.png"))

    def detail_title(self):
        return self.page.evaluate("document.querySelector('#detail h2')?.textContent ?? null")

    def wait_title(self, title):
        self.page.wait_for_function("t=>document.querySelector('#detail h2')?.textContent===t", arg=title, timeout=5000)

    def tick(self, ms=15000):
        self.page.clock.run_for(ms)


class SelectionTest(Base):
    items = [item("visitor.a", Status.WARN, value="2.0%"), item("visitor.b", value="1.0%"),
             item("visitor.c", value="1.2%"), item("function.f1", category="function", value="1.0%"),
             item("function.f2", category="function", value="1.5%")]

    def test_selection_kept_and_updated(self):
        self.put(self.items)
        self.page.goto(self.url)
        self.page.click('[role=tab][data-tab-id=function]')
        self.page.click('.tile[data-key="function.f2"]')
        self.wait_title("項目 function.f2")
        self.put([*[i for i in self.items if i.key != "function.f2"], item("function.f2", category="function", value="7.7%")])
        for _ in range(2):
            self.tick()
            self.page.wait_for_timeout(300)
        self.page.wait_for_function("document.querySelector('#detail .dv')?.textContent==='7.7%'", timeout=5000)
        self.assertEqual(self.detail_title(), "項目 function.f2")
        self.assertEqual(self.page.get_attribute('[role=tab][data-tab-id=function]', 'aria-selected'), "true")
        self.assertEqual(self.page.get_attribute('.tile[data-key="function.f2"]', 'aria-pressed'), "true")

    def test_late_response_of_previous_selection_is_discarded(self):
        self.put(self.items)
        held = []
        self.page.route("**/fragment?key=visitor.b", lambda r: held.append(r))
        self.page.goto(self.url)
        self.wait_title("項目 visitor.a")
        self.page.click('.tile[data-key="visitor.b"]')
        self.page.click('.tile[data-key="visitor.c"]')
        self.wait_title("項目 visitor.c")
        for r in held:
            r.continue_()
        self.page.wait_for_timeout(500)
        self.assertEqual(len(held), 1)
        self.assertEqual(self.detail_title(), "項目 visitor.c")


class AutoSelectTest(Base):
    items = [item("visitor.a", Status.OK), item("visitor.b", Status.WARN), item("visitor.c", Status.WARN),
             item("function.f1", Status.CRIT, "function"), item("function.f2", Status.OK, "function")]

    def test_auto_select_follows_worst_and_user_selection_wins(self):
        self.put(self.items)
        self.page.goto(self.url)
        self.wait_title("項目 visitor.b")
        self.page.click('[role=tab][data-tab-id=function]')
        self.wait_title("項目 function.f1")
        self.page.click('[role=tab][data-tab-id=visitor]')
        self.wait_title("項目 visitor.b")
        self.put([item("visitor.a", Status.OK), item("visitor.b", Status.WARN), item("visitor.c", Status.CRIT),
                  *self.items[3:]])
        self.tick()
        self.wait_title("項目 visitor.c")
        self.page.click('.tile[data-key="visitor.a"]')
        self.wait_title("項目 visitor.a")
        self.put([item("visitor.a", Status.OK), item("visitor.b", Status.CRIT), item("visitor.c", Status.WARN),
                  *self.items[3:]])
        self.tick()
        self.page.wait_for_timeout(500)
        self.assertEqual(self.detail_title(), "項目 visitor.a")
        self.page.click('[role=tab][data-tab-id=function]')
        self.page.wait_for_timeout(500)
        self.assertEqual(self.detail_title(), "項目 visitor.a")
        self.page.click('[role=tab][data-tab-id=visitor]')
        self.page.keyboard.press("Escape")
        self.wait_title("項目 visitor.b")


# --- 寸法の予算 (design.md FestivalPage) ---
TOL = 0.6
NOTE = "注記は 2 行まで折り返して出し、それを超える分は省略する。" * 2
BD_ROWS = 13


def history(keys, minutes=60, gap=None):
    """直近 minutes 分の 1 分値。gap は欠測にする分の番号の範囲 (range)。JST の日付の範囲に収まる分だけを返す。"""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    jst0 = now.astimezone(timezone(timedelta(hours=9))).replace(hour=0, minute=0).astimezone(timezone.utc)
    rows = []
    for k in keys:
        for m in range(1, minutes + 1):
            t = now - timedelta(minutes=m)
            if t >= jst0 and not (gap and m in gap):
                rows.append((k, store_mod.model.iso(t), 0.01 * (m % 7 + 1)))
    return rows


def full_item(key, cat):
    """グラフ・閾値・2 行の注記・内訳 13 行・リンクをすべて持つ項目 (詳細の予算が最大になる)。"""
    bd = Breakdown("fest.bd.pod", ("fest.col.pod", "fest.col.role", "fest.col.ready"),
                   tuple((f"pod-{n}", "app", "fest.bool.true") for n in range(BD_ROWS)))
    return item(key, Status.WARN, cat, "2.0%", unit="ratio", warn=0.03, crit=0.05, metric=0.02, note=NOTE,
                breakdown=bd, link="https://example.com/x", link_label="fest.link.netdata")


def tab_items(cat, n):
    out = [full_item(f"{cat}.i00", cat)]
    for i in range(1, n):
        k = f"{cat}.i{i:02d}"
        if i % 3 == 1:
            out.append(item(k, Status.OK, cat, "1.0%", unit="ratio", warn=0.03, crit=0.05, metric=0.01, threshold=None))
        elif i % 3 == 2:
            out.append(item(k, Status.OK, cat, "稼働中 3/3 " + "とても長い現在値の続き " * 6, note="注記あり"))
        else:
            out.append(item(k, Status.OK, cat, "1.0%", unit="ratio", warn=0.03, crit=0.05, metric=0.01, note="注記あり",
                            observed_at=None))
    return out


def chart_keys(items):
    return [i.key for i in items if i.unit]


MEASURE_TILES = """()=>{
  const panel=[...document.querySelectorAll('.panel')].find(p=>!p.hidden);
  const r=e=>{const b=e.getBoundingClientRect();return{x:b.x,y:b.y,w:b.width,h:b.height}};
  return{panel:{...r(panel),sh:panel.scrollHeight,ch:panel.clientHeight,size:panel.dataset.size},
    detail:{sh:document.querySelector('#detail').scrollHeight,ch:document.querySelector('#detail').clientHeight},
    doc:{sh:document.documentElement.scrollHeight,ch:document.documentElement.clientHeight},
    tiles:[...panel.querySelectorAll('.tile')].map(t=>({...r(t),nc:t.classList.contains('nc'),
      sh:t.scrollHeight,ch:t.clientHeight,
      rows:Object.fromEntries(['h','v','sp','th','no','t'].map(c=>{const e=t.querySelector(':scope>.'+c);return[c,e?r(e):null]}))}))};
}"""

MEASURE_DETAIL = """()=>{
  const d=document.querySelector('#detail');
  const r=e=>{if(!e)return null;const b=e.getBoundingClientRect();return{x:b.x,y:b.y,w:b.width,h:b.height}};
  const q=s=>d.querySelector(s);
  return{d:{...r(d),sh:d.scrollHeight,ch:d.clientHeight},dh:r(q('.dh')),dv:r(q('.dv')),di:r(q('.di')),
    note:r([...d.querySelectorAll('.di dd')].pop()),
    dc:{...r(q('.dc')),sh:q('.dc')?.scrollHeight,ch:q('.dc')?.clientHeight},dch3:r(q('.dc h3')),svg:r(q('.dc svg')),gap:r(q('.dc .gap')),
    db:{...r(q('.db')),sh:q('.db')?.scrollHeight,ch:q('.db')?.clientHeight},dbh3:r(q('.db h3')),th:r(q('.db th')),
    tds:[...d.querySelectorAll('.db tbody tr')].map(tr=>r(tr.querySelector('td'))),
    more:d.querySelector('.db tr.more')?.textContent??null,dl:r(q('.dl'))};
}"""


class LayoutTest(Base):
    def open_n(self, n):
        items = [i for cat in TABS for i in tab_items(cat, n)]
        self.put(items, history(chart_keys(items)))
        self.page.goto(self.url)

    def goto_tab(self, cat):
        self.page.click(f'[role=tab][data-tab-id={cat}]')
        self.wait_title(f"項目 {cat}.i00")

    def check_tile_rows(self, n, m):
        g = tile_grid(n)
        self.assertEqual(m["panel"]["size"], g.size)
        self.assertEqual(len(m["tiles"]), n)
        px, py = m["panel"]["x"] + 12, m["panel"]["y"] + 12
        for idx, t in enumerate(m["tiles"]):
            with self.subTest(n=n, tile=idx):
                self.assertAlmostEqual(t["w"], g.w, delta=TOL)
                self.assertAlmostEqual(t["h"], g.h, delta=TOL)
                self.assertAlmostEqual(t["x"], px + (idx % g.cols) * (g.w + 8), delta=TOL)
                self.assertAlmostEqual(t["y"], py + (idx // g.cols) * (g.h + 8), delta=TOL)
                self.assertLessEqual(t["sh"], t["ch"])
                self.check_rows(g, t)

    def check_rows(self, g, t):
        rows, nc = t["rows"], t["nc"]
        if g.size == "standard":
            s = max(40, g.h - 158)
            y = t["y"] + 1 + 10
            v_h = 42 + s if nc else 38
            bands = {"h": (y, 24), "v": (y + 28, v_h)}
            nxt = y + 28 + v_h + 4
            if not nc:
                bands["sp"] = (y + 28 + 38 + 4, s)
                nxt = y + 28 + 38 + 4 + s + 4
            h_th = 18
            exact = ("h", "v", "sp", "th", "no", "t")
        else:
            r = max(28, g.h - 80)
            y = t["y"] + 1 + 4
            bands = {"h": (y, 20), "v": (y + 22, r)}
            if not nc:
                bands["sp"] = (y + 22, r)
            nxt = y + 22 + r + 2
            h_th = 14
            exact = ("h", "sp", "th", "no", "t")
        for name, ty in zip(("th", "no", "t"), (nxt, nxt + h_th + (4 if g.size == "standard" else 2),
                                              nxt + 2 * (h_th + (4 if g.size == "standard" else 2)))):
            bands[name] = (ty, h_th)
        for name, (top, h) in bands.items():
            b = rows[name]
            self.assertIsNotNone(b, name)
            self.assertGreaterEqual(b["y"], top - TOL, name)
            self.assertLessEqual(b["y"] + b["h"], top + h + TOL, name)
            if name in exact:
                self.assertAlmostEqual(b["h"], h, delta=TOL, msg=name)
        if nc:
            self.assertIsNone(rows["sp"])
        last = rows["t"]
        self.assertLessEqual(last["y"] + last["h"], t["y"] + t["h"] - 1 - (10 if g.size == "standard" else 4) + TOL)

    def test_no_scroll_and_tile_budget_for_each_count(self):
        for n in (1, 12, 20, 24, 42):
            self.open_n(n)
            for cat in TABS:
                self.goto_tab(cat)
                m = self.page.evaluate(MEASURE_TILES)
                with self.subTest(n=n, tab=cat):
                    self.assertLessEqual(m["panel"]["sh"], m["panel"]["ch"])
                    self.assertLessEqual(m["detail"]["sh"], m["detail"]["ch"])
                    self.assertLessEqual(m["doc"]["sh"], m["doc"]["ch"])
                    self.check_tile_rows(n, m)
            self.goto_tab("visitor")
            self.page.wait_for_timeout(200)
            self.page.screenshot(path=str(SHOTS / f"festival_size_{n}.png"))

    def test_detail_parts_fit_budget(self):
        self.open_n(12)
        self.goto_tab("function")
        m = self.page.evaluate(MEASURE_DETAIL)
        near = lambda a, b, msg: self.assertAlmostEqual(a, b, delta=TOL, msg=msg)
        near(m["dh"]["h"], 28, "dh")
        near(m["dv"]["h"], 36, "dv")
        near(m["di"]["h"], 110, "di")
        near(m["note"]["h"], 44, "note 2 行")
        near(m["dc"]["h"], 258, "dc")
        near(m["dch3"]["h"], 20, "dc h3")
        near(m["svg"]["w"], 528, "svg w")
        near(m["svg"]["h"], 220, "svg h")
        near(m["gap"]["h"], 18, "gap")
        self.assertLessEqual(m["dc"]["sh"], m["dc"]["ch"])
        near(m["dbh3"]["h"], 20, "db h3")
        near(m["th"]["h"], 28, "db th")
        self.assertEqual(len(m["tds"]), 11)
        for td in m["tds"]:
            near(td["h"], 26, "db td")
        self.assertEqual(m["more"], label("fest.detail.more_rows", n=BD_ROWS - 10))
        near(m["db"]["h"], 334, "db")
        self.assertLessEqual(m["db"]["sh"], m["db"]["ch"])
        near(m["dl"]["h"], 24, "link")
        # 部分の間隔は 1-2・2-3 が 8px、それ以外が 16px
        bottom = lambda k: m[k]["y"] + m[k]["h"]
        near(m["dh"]["y"], m["d"]["y"] + 16, "top padding")
        near(m["dv"]["y"], bottom("dh") + 8, "dh-dv")
        near(m["di"]["y"], bottom("dv") + 8, "dv-di")
        near(m["dc"]["y"], bottom("di") + 16, "di-dc")
        near(m["db"]["y"], bottom("dc") + 16, "dc-db")
        near(m["dl"]["y"], bottom("db") + 16, "db-link")
        self.assertLessEqual(bottom("dl"), m["d"]["y"] + m["d"]["h"] - 16 + TOL)
        self.assertLessEqual(m["d"]["sh"], m["d"]["ch"])


class DetailChartTest(Base):
    def setUp(self):
        super().setUp()
        self.items = tab_items("visitor", 3)
        self.keys = chart_keys(self.items)

    def open(self, gap=None):
        mins = (datetime.now(timezone.utc) + timedelta(hours=9)).hour * 60 + (datetime.now(timezone.utc) + timedelta(hours=9)).minute
        if mins < 30:
            self.skipTest("JST の日付変更直後は当日の記録が少なく、欠測を作れない")
        self.put(self.items, history(self.keys, minutes=20, gap=gap))
        self.page.goto(self.url)

    def test_select_shows_chart_and_breakdown_without_navigation(self):
        self.open()
        navs = []
        self.page.on("framenavigated", lambda f: navs.append(f.url))
        self.page.evaluate("window.__marker = 1")
        self.page.click('.tile[data-key="visitor.i01"]')
        self.wait_title("項目 visitor.i01")
        self.assertEqual(self.page.evaluate("window.__marker"), 1)
        self.assertEqual(navs, [])
        self.assertEqual(self.page.locator("#detail .db").count(), 0)
        self.assertEqual(self.page.locator("#detail svg.daychart").count(), 1)
        self.page.click('.tile[data-key="visitor.i00"]')
        self.wait_title("項目 visitor.i00")
        self.assertEqual(self.page.evaluate("window.__marker"), 1)
        self.assertEqual(navs, [])
        svg = self.page.locator("#detail svg.daychart")
        self.assertEqual(svg.count(), 1)
        self.assertEqual(svg.locator('line[stroke-dasharray="6 4"]').count(), 1)
        self.assertEqual(svg.locator('line[stroke-dasharray="2 3"]').count(), 1)
        texts = svg.locator("text").evaluate_all("els=>els.map(e=>e.textContent)")
        self.assertIn(label("fest.detail.chart_warn", value="3%"), texts)
        self.assertIn(label("fest.detail.chart_crit", value="5%"), texts)
        self.assertEqual(self.page.locator("#detail .db tbody tr").count(), 11)
        self.assertEqual(self.page.locator("#detail .gap").inner_text(), label("fest.detail.chart_gap"))
        self.assertEqual(self.page.locator("#detail a.dlink").get_attribute("target"), "_blank")
        self.assertEqual(self.page.locator("#detail a.dlink").inner_text(), label("fest.detail.link", name=label("fest.link.netdata")))

    def value_runs(self):
        d = self.page.get_attribute('#detail svg.daychart path[style*="--fg"]', "d")
        return d.count("M")

    def test_line_is_continuous_without_gap(self):
        self.open()
        self.wait_title("項目 visitor.i00")
        self.assertEqual(self.value_runs(), 1)

    def test_line_breaks_at_missing_minutes(self):
        self.open(gap=range(8, 13))
        self.wait_title("項目 visitor.i00")
        self.assertEqual(self.value_runs(), 2)


# --- 失敗時の表示と、タブ見出し・帯の一致 ---
def status_fixture():
    return [
        *(item(f"visitor.c{n}", Status.CRIT, "visitor") for n in range(1, 4)), item("visitor.ok", Status.OK, "visitor"),
        *(item(f"function.c{n}", Status.CRIT, "function") for n in range(1, 4)), item("function.w", Status.WARN, "function"),
        item("capacity.e", Status.ERROR, "capacity"), item("capacity.w", Status.WARN, "capacity"),
        item("capacity.info", Status.OK, "capacity", judged=False),
        item("infra.s", Status.STALE, "infra"), item("infra.w", Status.WARN, "infra"),
    ]


READ_HEADS = """()=>({counts:document.querySelector('.counts').textContent,
  names:[...document.querySelectorAll('.names li')].map(e=>e.textContent),
  tabs:Object.fromEntries([...document.querySelectorAll('[role=tab]')].map(t=>[t.dataset.tabId,
    {badge:t.querySelector('.badge').textContent.trim(),cnt:t.querySelector('.cnt').textContent}])),
  critTiles:[...document.querySelectorAll('.tile.s-crit[data-rank]')].length,
  warnTiles:[...document.querySelectorAll('.tile.s-warn[data-rank]')].length})"""


class FailureTest(Base):
    items = status_fixture()

    def open(self):
        self.put(self.items)
        self.page.goto(self.url)
        self.wait_title("項目 visitor.c1")
        self.before = self.snapshot()

    def snapshot(self):
        return self.page.evaluate("""()=>({title:document.querySelector('#detail h2')?.textContent,
          dv:document.querySelector('#detail .dv')?.textContent,tiles:document.querySelectorAll('.tile').length,
          tab:document.querySelector('[role=tab][aria-selected=true]').dataset.tabId,
          sel:document.querySelector('.tile[aria-pressed=true]')?.dataset.key,
          counts:document.querySelector('.counts').textContent})""")

    def fail(self, handler):
        self.page.route("**/admin/festival/fragment*", handler)
        self.tick()
        self.page.wait_for_function("!document.querySelector('#festival-warn').hidden", timeout=5000)

    def check_kept(self):
        self.assertEqual(self.snapshot(), self.before)

    def test_tab_heads_match_band(self):
        self.open()
        h = self.page.evaluate(READ_HEADS)
        self.assertEqual(h["counts"], label("fest.band.counts", crit=6, warn=3))
        self.assertEqual((h["critTiles"], h["warnTiles"]), (6, 3))
        self.assertEqual(h["names"], ["項目 function.c1", "項目 function.c2", "項目 function.c3", "項目 visitor.c1",
                                      "項目 visitor.c2", label("fest.band.more", n=1)])
        expect = {"visitor": ("異常", 3), "function": ("異常", 3), "capacity": ("取得失敗", 0), "infra": ("注意", 0)}
        for tab, (worst, n) in expect.items():
            self.assertIn(label("fest.status." + {"異常": "crit", "取得失敗": "error", "注意": "warn"}[worst]), h["tabs"][tab]["badge"])
            self.assertEqual(h["tabs"][tab]["cnt"], f'{label("fest.status.crit")} {n} 件')
        self.assertEqual(sum(n for _, n in expect.values()), 6)

    def test_info_only_tab_shows_dash_and_is_excluded_from_band(self):
        self.put([item("visitor.a", Status.OK, "visitor"), item("capacity.i1", Status.OK, "capacity", judged=False),
                  item("capacity.i2", Status.ERROR, "capacity", judged=False)])
        self.page.goto(self.url)
        h = self.page.evaluate(READ_HEADS)
        self.assertEqual(h["tabs"]["capacity"]["badge"], "–")
        self.assertEqual(h["tabs"]["infra"]["badge"], "–")
        self.assertEqual(h["tabs"]["capacity"]["cnt"], f'{label("fest.status.crit")} 0 件')
        self.assertEqual(h["counts"], label("fest.band.counts", crit=0, warn=0))
        self.assertEqual(h["names"], [])

    def test_401_warns_with_login_link_and_keeps_display(self):
        self.open()
        self.fail(lambda r: r.fulfill(status=401, body="unauthorized"))
        w = self.page.locator("#festival-warn")
        self.assertIn(label("fest.band.reauth"), w.inner_text())
        self.assertEqual(w.locator("a").get_attribute("href"), "/admin/festival")
        self.check_kept()
        self.assertEqual(self.page.evaluate(READ_HEADS)["counts"], label("fest.band.counts", crit=6, warn=3))
        self.shot_as("festival_fail_401.png")

    def test_500_warns_stale_and_strengthens_after_45s(self):
        self.open()
        self.fail(lambda r: r.fulfill(status=500, body="boom"))
        w = self.page.locator("#festival-warn")
        self.assertRegex(w.inner_text(), r"^表示が古い \(最終更新 \d\d:\d\d:\d\d\)$")
        self.assertNotIn("strong", w.get_attribute("class"))
        self.check_kept()
        self.shot_as("festival_fail_500.png")
        self.tick(46000)
        self.page.wait_for_function("document.querySelector('#festival-warn').classList.contains('strong')", timeout=5000)
        self.check_kept()

    def test_network_failure_warns_stale_and_recovers(self):
        self.open()
        self.fail(lambda r: r.abort("connectionrefused"))
        self.assertRegex(self.page.locator("#festival-warn").inner_text(), r"^表示が古い \(最終更新 \d\d:\d\d:\d\d\)$")
        self.check_kept()
        self.shot_as("festival_fail_network.png")
        self.page.unroute("**/admin/festival/fragment*")
        self.tick()
        self.page.wait_for_function("document.querySelector('#festival-warn').hidden", timeout=5000)
        self.check_kept()

    def shot_as(self, name):
        self.page.screenshot(path=str(SHOTS / name))


if __name__ == "__main__":
    unittest.main()
