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

    def test_dense_when_over_threshold(self):
        n = page.DENSE_THRESHOLD
        normal = self.frag([item(f"capacity.{i:03}", category="capacity") for i in range(n)])
        self.assertNotIn('data-dense="true"', normal)
        dense = self.frag([item(f"capacity.{i:03}", category="capacity") for i in range(n + 1)])
        self.assertEqual(dense.count('data-dense="true"'), 1)

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
