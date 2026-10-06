import unittest
from datetime import datetime, timezone

from helpers import FIXED_NOW
from model import Item, SourceResult, Status, make_result
from render import components as c
from render import labels, page, svg
from render.sections import Section, load_sections


def sect(snapshot, query, sid="x.src"):
    r = snapshot.get(sid)
    return str(c.card("テスト節", r, lambda items: c.table(["名", "状態"], [[i.label, c.status_cell(i)] for i in items]),
                      source="例"))


S = Section("demo", "nav.billing", "sec.billing", ("x.src",), sect)


def res(status, items=(), error=None, last=FIXED_NOW):
    return SourceResult("x.src", status, FIXED_NOW, last, tuple(items), error)


class Labels(unittest.TestCase):
    def test_all_design_keys_present(self):
        for k in ("status.error", "dash.summary.counts", "legend.otp_sms", "disposition.reject",
                  "nav.dmarc", "note.falco_ingest_failed", "period.open", "chart.falco_rule.y", "sec.fail2ban"):
            self.assertIn(k, labels.LABELS)
        self.assertEqual(labels.label("status.error"), "取得失敗")

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            labels.label("no.such.key")


class Components(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(c.fmt_dt(datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)), "2026-06-01 21:00")
        self.assertEqual(c.fmt_int(1234567), "1,234,567")
        self.assertEqual(c.fmt_bytes(1.5e12), "1.50 TB")
        self.assertEqual(c.fmt_bytes(2.5e9), "2.5 GB")

    def test_every_status_badge_has_text(self):
        for st in Status:
            self.assertIn(labels.label("status." + st.value), c.badge(st))

    def test_escape(self):
        self.assertNotIn("<script>", c.table(["h"], [["<script>"]]))

    def test_error_card_distinct_and_keeps_previous(self):
        item = Item("k", "前回の値", Status.OK, {})
        out = c.card("T", res(Status.ERROR, [item], "HTTPError: 503"), lambda items: items[0].label)
        self.assertIn("取得失敗", out)
        self.assertIn("取得できませんでした (HTTPError: 503)", out)
        self.assertIn("最終成功: 2026-06-01 21:00", out)
        self.assertIn('class="prev"', out)
        self.assertIn("s-error", out)

    def test_error_without_items_has_no_empty_message(self):
        out = c.card("T", res(Status.ERROR, error="x", last=None), lambda items: "")
        self.assertNotIn("該当するデータはありません", out)
        self.assertNotIn("最終成功", out)

    def test_ok_card_not_marked_error(self):
        out = c.card("T", res(Status.OK, [Item("k", "行", Status.OK, {})]), lambda items: "本文")
        self.assertNotIn("取得失敗", out)
        self.assertNotIn('class="prev"', out)

    def test_empty_and_collecting(self):
        self.assertIn("該当するデータはありません", c.card("T", res(Status.EMPTY), lambda i: ""))
        self.assertIn("データを収集中です", c.card("T", None, lambda i: ""))

    def test_pick_range_and_nav(self):
        self.assertEqual(c.pick_range({"falco": ["bogus"]}, "falco", ["24h", "7d"]), "24h")
        nav = c.range_nav("falco", "falco", ["24h", "7d"], {"falco": ["7d"], "auth": ["24h"]})
        self.assertIn("auth=24h&amp;falco=24h#sec-falco", nav)
        self.assertEqual(nav.count("aria-current"), 1)


class Charts(unittest.TestCase):
    def test_bar_chart_has_svg_legend_and_numbers_table(self):
        out = svg.bar_chart("件数", "日時", "件数 (件)", ["06-01", "06-02"], [("警告", [1, 0]), ("エラー", [2, 3])])
        self.assertIn("<svg", out)
        self.assertIn("<figcaption>件数</figcaption>", out)
        self.assertIn("数値を表で見る", out)
        self.assertIn("<td>06-01</td><td class=num>1</td><td class=num>2</td>", out)
        self.assertIn("エラー", out)

    def test_empty_chart_renders(self):
        self.assertIn("<svg", svg.bar_chart("t", "x", "y", [], []))
        self.assertIn("<svg", svg.hbar_chart("t", "x", "y", []))

    def test_hbar_numbers(self):
        out = svg.hbar_chart("上位", "検知件数 (件)", "ルール", [("A", 5), ("B", 1200)])
        self.assertIn("1,200", out)


class Page(unittest.TestCase):
    def render(self, snap, query=None, sections=(S,)):
        return page.render(snap, query or {}, sections=list(sections))

    def test_frame(self):
        out = self.render({"x.src": res(Status.OK, [Item("k", "行", Status.OK, {})])})
        for t in ("運用ダッシュボード", "最終更新: 2026-06-01 21:00", "再読み込み", "ポータルへ戻る", "ログアウト",
                  'href="#sec-demo"', "要確認の項目はありません", 'id="sec-demo"'):
            self.assertIn(t, out)

    def test_summary_counts_and_links(self):
        snap = {"x.src": res(Status.WARN, [Item("a", "A", Status.WARN, {}, "注意"), Item("b", "B", Status.CRIT, {})]),
                "y.other": res(Status.ERROR, error="boom")}
        out = self.render(snap)
        self.assertIn("異常 1 件 / 警告 1 件 / 取得失敗 1 件", out)
        self.assertIn('<a href="#sec-demo">', out)

    def test_error_source_visibly_differs_from_ok(self):
        ok = self.render({"x.src": res(Status.OK, [Item("k", "行", Status.OK, {})])})
        bad = self.render({"x.src": res(Status.ERROR, [Item("k", "行", Status.OK, {})], "Timeout")})
        self.assertNotIn("取得失敗", ok)
        self.assertIn("取得失敗", bad)
        self.assertIn("取得できませんでした (Timeout)", bad)
        self.assertIn("異常 0 件 / 警告 0 件 / 取得失敗 1 件", bad)
        # 取得失敗の直前の値は要確認の集計に入れない
        self.assertIn("異常 0 件 / 警告 0 件 / 取得失敗 1 件",
                      self.render({"x.src": res(Status.ERROR, [Item("k", "行", Status.CRIT, {})], "T")}))

    def test_section_exception_is_contained(self):
        def boom(snap, q):
            raise ValueError("bad")
        out = self.render({}, sections=[S, Section("boom", "nav.node", "sec.node", (), boom)])
        self.assertIn("ValueError", out)
        self.assertIn('id="sec-demo"', out)

    def test_no_snapshot_shows_collecting(self):
        self.assertIn("データを収集中です", self.render({}))

    def test_loader_skips_only_missing_modules(self):
        self.assertEqual(load_sections(("no_such_section",)), [])
        self.assertIsInstance(load_sections(), list)

    def test_render_with_defaults(self):
        self.assertIn("運用ダッシュボード", page.render({"a": make_result("a", FIXED_NOW, [])}, {}))


if __name__ == "__main__":
    unittest.main()
