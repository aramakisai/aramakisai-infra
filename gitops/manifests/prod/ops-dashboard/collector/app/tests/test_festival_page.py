import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import config
import store as store_mod
from festival import page
from festival.labels import label
from model import Breakdown, FestivalItem, Status

FEST = config.load(os.path.join(os.path.dirname(__file__), "..", "..", "dashboard.toml")).festival
NOW = datetime(2026, 11, 14, 3, 0, 0, tzinfo=timezone.utc)  # JST 12:00:00


def item(key, status=Status.OK, category="visitor", **kw):
    base = dict(key=key, category=category, label=f"名前 {key}", status=status, value="1.5%",
                threshold="注意 ≥ 1.0% / 異常 ≥ 5.0%", observed_at=NOW - timedelta(minutes=3),
                fetched_at=NOW - timedelta(seconds=5), source_ids=("festival.cloudflare",),
                link="/admin/#sec-cluster", note="メモ <b>", notify=True, judged=True)
    base.update(kw)
    return FestivalItem(**base)


class FestivalPageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store_mod.Store(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def frag(self, items, at=NOW, now=NOW, **kw):
        self.store.replace_festival_items(items, at)
        return page.render_fragment(self.store, FEST, now, **kw)

    def test_band_counts_and_critical_names_match_tabs(self):
        html = self.frag([
            item("visitor.a", Status.CRIT, label="ワーカー"), item("visitor.b", Status.WARN),
            item("function.c", Status.CRIT, "function", label="駐車場"),
            item("capacity.d", Status.OK, "capacity"), item("infra.e", Status.WARN, "infra"),
            item("capacity.cache", Status.OK, "capacity", judged=False),
        ])
        band = re.search(r'<header id="festival-band".*?</header>', html, re.S).group(0)
        self.assertIn("異常 2 件 / 注意 2 件", band)
        self.assertIn("ワーカー", band)
        self.assertIn("駐車場", band)
        self.assertNotIn("名前 visitor.b", band)
        self.assertIn("最終更新 12:00:00", band)
        tabs = dict(re.findall(r'<button[^>]*role="tab"[^>]*data-tab-id="(\w+)"[^>]*>(.*?)</button>', html, re.S))
        self.assertIn("異常 1", re.sub(r"<[^>]+>", " ", tabs["visitor"]))
        self.assertIn("異常", tabs["visitor"])
        self.assertIn("異常", tabs["function"])
        self.assertIn("正常", tabs["capacity"])
        self.assertIn("注意", tabs["infra"])
        self.assertIn("来場者影響", tabs["visitor"])
        self.assertEqual(list(tabs), ["visitor", "traffic", "function", "capacity", "infra"])
        self.assertIn("トラフィック", tabs["traffic"])

    def test_display_only_item_is_not_counted_and_has_no_ok_color(self):
        html = self.frag([item("capacity.cache", Status.OK, "capacity", judged=False)])
        self.assertNotIn("表示のみ", html)
        tile = re.search(r'<button type="button" class="tile[^"]*"[^>]*data-key="capacity.cache".*?</button>', html, re.S).group(0)
        self.assertIn("s-info", tile)
        self.assertNotIn("s-ok", tile)
        self.assertIn("異常 0 件 / 注意 0 件", html)
        capacity_tab = re.search(r'role="tab"[^>]*data-tab-id="capacity".*?</button>', html, re.S).group(0)
        self.assertNotIn("s-ok", capacity_tab)

    def test_status_chip_is_icon_only_with_aria_label(self):
        html = self.frag([item("visitor.a", Status.CRIT)], key="visitor.a")
        tile = re.search(r'<button type="button" class="tile[^"]*"[^>]*data-key="visitor.a".*?</button>', html, re.S).group(0)
        self.assertIn('<span class="badge s-crit" role="img" aria-label="異常"><span aria-hidden="true">✖</span></span>', tile)
        self.assertNotIn("</span> 異常", html)
        self.assertIn('aria-label="異常"', re.search(r'<div class="dh">.*?</div>', html, re.S).group(0))
        self.assertIn('aria-label="異常"', re.search(r'role="tab"[^>]*data-tab-id="visitor".*?</button>', html, re.S).group(0))

    def test_display_only_has_no_chip_in_tile_and_detail_but_keeps_dashed_edge(self):
        html = self.frag([item("capacity.cache", Status.OK, "capacity", judged=False)], key="capacity.cache")
        self.assertNotIn("badge", re.search(r'<button type="button" class="tile[^"]*"[^>]*data-key="capacity.cache".*?</button>',
                                            html, re.S).group(0))
        self.assertNotIn("badge", re.search(r'<div class="dh">.*?</div>', html, re.S).group(0))
        self.assertIn("tile s-info", html)

    def test_traffic_tab_order(self):
        hosts = FEST.cloudflare.traffic_hosts
        keys = ["traffic.node.b.net_tx", "traffic.node.a.net_tx", "traffic.node.a.net_rx", "traffic.cms.rps",
                "traffic.worker.requests", "traffic.host.other", f"traffic.host.{hosts[2]}", f"traffic.host.{hosts[0]}",
                "traffic.total"]
        html = self.frag([item(k, category="traffic", judged=False) for k in keys])
        panel = re.search(r'id="panel-traffic".*?</div>', html, re.S).group(0)
        self.assertEqual(re.findall(r'data-key="([^"]+)"', panel),
                         ["traffic.total", f"traffic.host.{hosts[0]}", f"traffic.host.{hosts[2]}", "traffic.host.other",
                          "traffic.worker.requests", "traffic.cms.rps", "traffic.node.a.net_rx", "traffic.node.a.net_tx",
                          "traffic.node.b.net_tx"])

    def test_display_only_with_stale_shows_stale(self):
        html = self.frag([item("capacity.cache", Status.STALE, "capacity", judged=False)])
        self.assertIn("取得遅延", html)
        self.assertNotIn("表示のみ", html)

    def test_tile_shows_name_status_value_and_fetched_time_in_jst(self):
        html = self.frag([item("visitor.a", Status.WARN, fetched_at=NOW - timedelta(seconds=5))])
        tile = re.search(r'<button type="button" class="tile.*?</button>', html, re.S).group(0)
        for s in ("名前 visitor.a", "注意", "1.5%", "11:59:55"):
            self.assertIn(s, tile)
        self.assertIn('data-tab="visitor"', tile)
        self.assertIn('aria-controls="detail"', tile)

    def detail(self, key, items=None, **kw):
        if items is not None:
            self.store.replace_festival_items(items, NOW)
        html = page.render_fragment(self.store, FEST, NOW, key=key, **kw)
        m = re.search(r'<aside id="detail" aria-live="polite"[^>]*>(.*)</aside>', html, re.S)
        return html, m.group(1)

    def test_detail_has_threshold_times_escaped_note_and_no_template(self):
        html, d = self.detail("visitor.a", [item("visitor.a", Status.CRIT)])
        self.assertNotIn("<template", html)
        self.assertIn('data-key="visitor.a"', html.split(">", 1)[0])
        self.assertIn("注意 ≥ 1.0% / 異常 ≥ 5.0%", d)
        self.assertIn("11:57:00", d)
        self.assertIn("11:59:55", d)
        self.assertIn("メモ &lt;b&gt;", d)
        self.assertNotIn("<b>", d)
        self.assertIn("名前 visitor.a", d)
        self.assertIn("1.5%", d)
        self.assertNotIn("<svg", d)
        self.assertNotIn("<table", d)

    def test_detail_unselected_when_no_key(self):
        html, d = self.detail(None, [item("visitor.a")])
        self.assertIn(label("fest.detail.empty"), d)
        self.assertNotIn("名前 visitor.a", d)
        self.assertIn('data-key=""', html.split(">", 1)[0])

    def test_detail_gone_for_unknown_or_long_key(self):
        for key in ("visitor.zzz", "k" * 201):
            _, d = self.detail(key, [item("visitor.a")])
            self.assertIn("この項目は現在ありません", d)

    def test_detail_escapes_key_and_passes_it_as_sql_parameter(self):
        evil = '"><script>alert(1)</script>\' OR 1=1 --'
        html, d = self.detail(evil, [item("visitor.a")])
        self.assertNotIn("<script>alert", html)
        self.assertIn("この項目は現在ありません", d)
        self.assertEqual(self.store.query("SELECT COUNT(*) FROM festival_items")[0][0], 1)

    def test_detail_key_value_is_escaped_in_root_attribute(self):
        html = page.render_fragment(self.store, FEST, NOW, key='a"b<c')
        root = html.split(">", 1)[0]
        self.assertIn('data-key="a&quot;b&lt;c"', root)

    def test_detail_only_renders_selected_item(self):
        _, d = self.detail("visitor.a", [item("visitor.a"), item("visitor.b", note="別の注記")])
        self.assertNotIn("別の注記", d)

    def test_detail_day_chart_reads_today_jst_for_key_without_thinning(self):
        calls = []
        orig = self.store.load_festival_history
        self.store.load_festival_history = lambda *a, **k: calls.append((a, k)) or orig(*a, **k)
        it = item("visitor.a", metric=0.5, unit="ratio", warn=0.8, crit=0.9)
        self.store.replace_festival_items([it], NOW)
        for m in (0, 1, 5):  # JST 11:55 / 11:56 / 12:00 のうち 2 分目の欠測を含む
            at = NOW - timedelta(minutes=5 - m)
            self.store.replace_festival_items([it], at, history=[("visitor.a", 0.1 * (m + 1))], history_minute=at)
        self.store.replace_festival_items([it], NOW)
        _, d = self.detail("visitor.a")
        self.assertIn('class="daychart"', d)
        self.assertIn("名前 visitor.a の当日の推移", d)
        self.assertIn("当日の推移 (00:00〜12:00)", d)
        self.assertNotIn('class="gap"', d)
        self.assertNotIn("線の途切れ", d)
        self.assertIn("注意 80%", d)
        self.assertIn("異常 90%", d)
        self.assertEqual(d.count("M"), 2)  # 欠測で 2 本の線に分かれる
        since, until = calls[-1][0]
        self.assertEqual(calls[-1][1], {"key": "visitor.a"})
        self.assertEqual(since, datetime(2026, 11, 13, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(until, NOW)

    def test_detail_chart_without_crit_draws_warn_line_only(self):
        it = item("visitor.a", metric=0.5, unit="ratio", warn=0.8, crit=None, threshold="注意 ≥ 80%")
        self.store.replace_festival_items([it], NOW, history=[("visitor.a", 0.5)], history_minute=NOW)
        _, d = self.detail("visitor.a")
        self.assertIn("注意 80%", d)
        self.assertNotIn("異常 ", d.split('class="daychart"')[1].split("</svg>")[0])

    def test_detail_chart_empty_and_absent_for_non_chart_item(self):
        _, d = self.detail("visitor.a", [item("visitor.a", metric=0.5, unit="ratio")])
        self.assertIn(label("fest.detail.chart_empty"), d)
        _, d = self.detail("visitor.b", [item("visitor.b")])
        self.assertNotIn("当日の推移", d)

    def test_detail_breakdown_resolves_labels_and_bool_keys(self):
        bd = Breakdown("fest.bd.pod", ("fest.col.pod", "fest.col.ready"),
                       (("cms-0", "fest.bool.true"), ("cms-1", "fest.bool.false"), ("cms-2", "—")))
        _, d = self.detail("visitor.a", [item("visitor.a", breakdown=bd)])
        for s in ("Pod ごと", "<th", "Pod", "Ready", "cms-0", "はい", "いいえ", "—"):
            self.assertIn(s, d)
        self.assertNotIn("fest.", d)
        self.assertNotIn("ほか", d)

    def test_detail_breakdown_over_11_rows_shows_first_10_and_remainder(self):
        rows = tuple((f"pod-{n:02}",) for n in range(14))
        _, d = self.detail("visitor.a", [item("visitor.a", breakdown=Breakdown("fest.bd.pod", ("fest.col.pod",), rows))])
        self.assertEqual(len(re.findall(r"<tr><td", d)), 10)
        self.assertIn("pod-09", d)
        self.assertNotIn("pod-10", d)
        self.assertIn("ほか 4 行", d)
        exact = tuple((f"pod-{n:02}",) for n in range(11))
        _, d = self.detail("visitor.a", [item("visitor.a", breakdown=Breakdown("fest.bd.pod", ("fest.col.pod",), exact))])
        self.assertIn("pod-10", d)
        self.assertNotIn("ほか", d)

    def test_detail_breakdown_cells_are_escaped(self):
        bd = Breakdown("fest.bd.pod", ("fest.col.pod",), (("<i>x</i>",),))
        _, d = self.detail("visitor.a", [item("visitor.a", breakdown=bd)])
        self.assertIn("&lt;i&gt;x&lt;/i&gt;", d)

    def test_detail_link_is_text_link_with_name_and_new_tab_for_external(self):
        _, d = self.detail("visitor.a", [item("visitor.a", link="https://app.netdata.cloud/", link_label="fest.link.netdata")])
        self.assertIn('<a class="dlink" href="https://app.netdata.cloud/" target="_blank" rel="noopener">Netdata を開く</a>', d)
        _, d = self.detail("visitor.a", [item("visitor.a", link="/admin/#sec-cluster", link_label="fest.link.admin")])
        self.assertIn('href="/admin/#sec-cluster"', d)
        self.assertIn("運用ダッシュボード を開く", d)
        self.assertNotIn("_blank", d)
        self.assertNotIn("btn", d)
        _, d = self.detail("visitor.a", [item("visitor.a", link=None, link_label=None)])
        self.assertNotIn("<a ", d)

    def test_javascript_link_is_not_rendered(self):
        html = self.frag([item("visitor.a", link="javascript:alert(1)")])
        self.assertNotIn("javascript:", html)

    def test_fragment_root_carries_evaluated_at_and_stale_flag(self):
        html = self.frag([item("visitor.a")])
        self.assertIn(f'id="festival-fragment" data-evaluated-at="2026-11-14T03:00:00Z" data-eval-stale="false"', html)
        late = page.render_fragment(self.store, FEST, NOW + timedelta(seconds=61))
        self.assertIn('data-eval-stale="true"', late)
        self.assertIn("判定が止まっている (最終判定 12:00:00)", late)

    def test_never_evaluated_is_stale(self):
        html = page.render_fragment(self.store, FEST, NOW)
        self.assertIn('data-eval-stale="true"', html)
        self.assertIn("判定が止まっている (最終判定 -)", html)

    def test_unconfigured(self):
        html = page.render_fragment(self.store, None, NOW)
        self.assertIn("当日監視の宣言がありません", html)
        self.assertNotIn('role="tab"', html)

    def test_tile_grid_matches_design_table(self):
        table = {1: (1, 1, 640, 360, "standard"), 2: (1, 2, 640, 360, "standard"), 4: (2, 2, 640, 360, "standard"),
                 6: (2, 3, 640, 288, "standard"), 8: (2, 4, 640, 214, "standard"), 12: (4, 3, 328, 288, "standard"),
                 16: (4, 4, 328, 214, "standard"), 20: (5, 4, 260, 214, "standard"), 24: (6, 4, 216, 214, "compact"),
                 30: (6, 5, 216, 169, "compact"), 37: (6, 7, 216, 118, "compact"), 42: (6, 7, 216, 118, "compact")}
        for n, want in table.items():
            g = page.tile_grid(n)
            self.assertEqual((g.cols, g.rows, g.w, g.h, g.size), want, n)
        self.assertIsNone(page.tile_grid(0))

    def test_tile_size_fills_window_with_tile_grid_as_minimum(self):
        want = {(1920, 1080, 1): (1336, 880), (1920, 1080, 2): (1336, 436), (1920, 1080, 4): (664, 436),
                (1920, 1080, 6): (664, 288), (1920, 1080, 8): (664, 214), (1920, 1080, 12): (328, 288),
                (1920, 1080, 20): (260, 214), (1920, 1080, 24): (216, 214), (1920, 1080, 37): (216, 118),
                (2197, 1267, 12): (397, 350), (2560, 1440, 12): (488, 408), (2560, 1440, 4): (984, 616)}
        for (w, h, n), size in want.items():
            self.assertEqual(page.tile_size(n, w, h), size, (w, h, n))
        # 領域が最小寸法より小さいときは tile_grid の寸法のまま
        self.assertEqual(page.tile_size(12, 1366, 768), (328, 288))
        self.assertIsNone(page.tile_size(0, 1920, 1080))

    def test_css_has_fluid_layout(self):
        css = page.CSS
        self.assertNotIn("1360px", css)
        self.assertNotIn("904px", css)
        self.assertIn("grid-template-columns:1fr 560px;grid-template-rows:120px 56px 1fr", css)
        self.assertIn("height:100vh", css)
        self.assertIn("minmax(var(--tile-w),1fr)", css)
        self.assertIn("minmax(var(--tile-h),1fr)", css)
        self.assertRegex(re.search(r"\.panels\{[^}]*}", css).group(0), r"min-width:0")
        for sel in (".panel{", "#detail{"):
            self.assertRegex(re.search(re.escape(sel) + r"[^}]*}", css).group(0), r"overflow:auto")

    def test_panel_carries_size_and_grid_variables(self):
        def panel(n):
            html = self.frag([item(f"capacity.{i:03}", category="capacity") for i in range(n)])
            return re.search(r'<div role="tabpanel"[^>]*data-tab-id="capacity"[^>]*>', html).group(0)
        std, small = panel(20), panel(21)
        self.assertIn('data-size="standard"', std)
        self.assertIn("--cols:5;--tile-w:260px;--tile-h:214px", std)
        self.assertIn('data-size="compact"', small)
        self.assertIn("--cols:3;--tile-w:440px;--tile-h:118px", small)
        self.assertNotIn("data-size", panel(0))

    def test_tile_shows_threshold_note_and_times(self):
        html = self.frag([item("visitor.a", Status.WARN)])
        tile = re.search(r'<button type="button" class="tile.*?</button>', html, re.S).group(0)
        self.assertIn("注意 ≥ 1.0% / 異常 ≥ 5.0%", tile)
        self.assertIn("メモ &lt;b&gt;", tile)
        self.assertIn("データ 11:57:00・取得 11:59:55", tile)
        no_obs = self.frag([item("visitor.a", observed_at=None, threshold=None, note=None)])
        tile = re.search(r'<button type="button" class="tile.*?</button>', no_obs, re.S).group(0)
        self.assertIn("取得 11:59:55", tile)
        self.assertNotIn("データ", tile)

    def test_sparkline_only_for_chart_items(self):
        for m in range(3):
            at = NOW - timedelta(minutes=2 - m)
            self.store.replace_festival_items(
                [item("visitor.a", metric=0.1 * m, unit="ratio"), item("visitor.b")], at,
                history=[("visitor.a", 0.1 * m)], history_minute=at)
        html = page.render_fragment(self.store, FEST, NOW)
        a = re.search(r'data-key="visitor.a".*?</button>', html, re.S).group(0)
        b = re.search(r'data-key="visitor.b".*?</button>', html, re.S).group(0)
        self.assertIn('class="spark"', a)
        self.assertNotIn("<svg", b)

    def test_non_chart_tile_shows_value_once(self):
        html = self.frag([item("visitor.b", value="合計 12,345 件 (固有の値)")])
        tile = re.search(r'<button type="button" class="tile.*?</button>', html, re.S).group(0)
        self.assertEqual(tile.count("合計 12,345 件 (固有の値)"), 1)

    def test_sparkline_reads_history_once(self):
        calls = []
        orig = self.store.load_festival_history
        self.store.load_festival_history = lambda *a, **k: calls.append((a, k)) or orig(*a, **k)
        self.frag([item(f"visitor.{i}", metric=1.0, unit="count") for i in range(5)])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("key", calls[0][1])
        self.assertEqual(len(calls[0][0]), 2)

    def test_page_includes_fragment_detail_frame_and_labels(self):
        self.store.replace_festival_items([item("visitor.a")], NOW)
        html = page.render_page(self.store, FEST, NOW)
        self.assertIn("<title>当日監視</title>", html)
        self.assertEqual(html.count('id="detail"'), 1)
        self.assertIn('id="festival-fragment"', html)
        self.assertIn('id="detail"', html)
        self.assertIn("表示が古い・再ログインが必要", html)
        self.assertNotIn("<script src", html)

    def test_page_passes_loading_label_to_script(self):
        self.store.replace_festival_items([item("visitor.a")], NOW)
        html = page.render_page(self.store, FEST, NOW)
        data = json.loads(re.search(r'id="festival-labels">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(data["loading"], "読み込み中")

    def test_tile_rank_follows_model_severity_and_is_absent_for_display_only(self):
        html = self.frag([item("visitor.a", Status.CRIT), item("visitor.b", Status.OK),
                          item("visitor.c", Status.WARN, judged=False)])
        rank = {k: re.search(rf'data-key="{k}"[^>]*', html).group(0) for k in ("visitor.a", "visitor.b", "visitor.c")}
        self.assertIn('data-rank="5"', rank["visitor.a"])
        self.assertIn('data-rank="1"', rank["visitor.b"])
        self.assertNotIn("data-rank", rank["visitor.c"])

    def test_labels_cover_design_table(self):
        self.assertEqual(label("fest.detail.link", name=label("fest.link.netdata")), "Netdata を開く")
        for k in ("fest.detail.note", "fest.detail.more_rows", "fest.detail.empty", "fest.link.cloudflare",
                  "fest.link.admin", "fest.link.cms", "fest.link.healthchecks"):
            self.assertTrue(label(k, n=1, name="x"))
        with self.assertRaises(KeyError):
            label("fest.nope")

    def test_band_time_is_zero_padded_24h(self):
        # 「最終更新 HH:MM:SS」は 1 桁の時と 0 時で崩れないよう、2 桁・h23 を明示する
        self.assertRegex(page.JS, r"hour:'2-digit'[^}]*hourCycle:'h23'|hourCycle:'h23'[^}]*hour:'2-digit'")


if __name__ == "__main__":
    unittest.main()
