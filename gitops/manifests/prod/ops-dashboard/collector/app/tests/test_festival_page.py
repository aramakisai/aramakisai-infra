import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import store as store_mod
from festival import page
from festival.labels import label
from model import FestivalItem, Status

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

    def frag(self, items, at=NOW, now=NOW):
        self.store.replace_festival_items(items, at)
        return page.render_fragment(self.store, object(), now)

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

    def test_display_only_item_is_not_counted_and_has_no_ok_color(self):
        html = self.frag([item("capacity.cache", Status.OK, "capacity", judged=False)])
        self.assertIn("表示のみ", html)
        tile = re.search(r'<button type="button" class="tile[^"]*"[^>]*data-key="capacity.cache".*?</button>', html, re.S).group(0)
        self.assertIn("s-info", tile)
        self.assertNotIn("s-ok", tile)
        self.assertIn("異常 0 件 / 注意 0 件", html)
        capacity_tab = re.search(r'role="tab"[^>]*data-tab-id="capacity".*?</button>', html, re.S).group(0)
        self.assertNotIn("s-ok", capacity_tab)

    def test_display_only_with_stale_shows_stale(self):
        html = self.frag([item("capacity.cache", Status.STALE, "capacity", judged=False)])
        self.assertIn("取得遅延", html)
        self.assertNotIn("表示のみ</span></button>", html)

    def test_tile_shows_name_status_value_and_fetched_time_in_jst(self):
        html = self.frag([item("visitor.a", Status.WARN, fetched_at=NOW - timedelta(seconds=5))])
        tile = re.search(r'<button type="button" class="tile.*?</button>', html, re.S).group(0)
        for s in ("名前 visitor.a", "注意", "1.5%", "11:59:55"):
            self.assertIn(s, tile)
        self.assertIn('data-tab="visitor"', tile)
        self.assertIn('aria-controls="detail"', tile)

    def test_detail_template_has_threshold_link_times_and_escaped_note(self):
        html = self.frag([item("visitor.a", Status.CRIT)])
        tpl = re.search(r'<template data-detail-for="visitor.a">(.*?)</template>', html, re.S).group(1)
        self.assertIn("注意 ≥ 1.0% / 異常 ≥ 5.0%", tpl)
        self.assertIn('href="/admin/#sec-cluster"', tpl)
        self.assertIn("詳細を見る", tpl)
        self.assertIn("11:57:00", tpl)
        self.assertIn("11:59:55", tpl)
        self.assertIn("メモ &lt;b&gt;", tpl)
        self.assertNotIn("<b>", tpl)

    def test_javascript_link_is_not_rendered(self):
        html = self.frag([item("visitor.a", link="javascript:alert(1)")])
        self.assertNotIn("javascript:", html)

    def test_fragment_root_carries_evaluated_at_and_stale_flag(self):
        html = self.frag([item("visitor.a")])
        self.assertIn(f'id="festival-fragment" data-evaluated-at="2026-11-14T03:00:00Z" data-eval-stale="false"', html)
        late = page.render_fragment(self.store, object(), NOW + timedelta(seconds=61))
        self.assertIn('data-eval-stale="true"', late)
        self.assertIn("判定が止まっている (最終判定 12:00:00)", late)

    def test_never_evaluated_is_stale(self):
        html = page.render_fragment(self.store, object(), NOW)
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
        html = page.render_fragment(self.store, object(), NOW)
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
        html = page.render_page(self.store, object(), NOW)
        self.assertIn("<title>当日監視</title>", html)
        self.assertIn('id="festival-fragment"', html)
        self.assertIn('id="detail"', html)
        self.assertIn("表示が古い・再ログインが必要", html)
        self.assertNotIn("<script src", html)

    def test_labels_cover_design_table(self):
        self.assertEqual(label("fest.status.info"), "表示のみ")
        with self.assertRaises(KeyError):
            label("fest.nope")


if __name__ == "__main__":
    unittest.main()
