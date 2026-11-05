import re
import unittest
from datetime import datetime, timedelta, timezone

from festival import chart
from festival.labels import label

JST = timezone(timedelta(hours=9))
D = datetime(2026, 11, 7, tzinfo=JST)


def at(h, m=0):
    return D.replace(hour=h, minute=m)


def paths(svg):
    return re.findall(r'<path d="([^"]*)"', svg)


class SparklineTest(unittest.TestCase):
    END = at(12, 30)

    def pts(self, offsets, v=lambda i: i):
        return [(self.END - timedelta(minutes=o), v(o)) for o in sorted(offsets, reverse=True)]

    def test_empty_is_blank(self):
        self.assertEqual(chart.sparkline([], self.END), "")

    def test_points_outside_window_are_blank(self):
        self.assertEqual(chart.sparkline(self.pts([60, 61]), self.END), "")

    def test_frame(self):
        s = chart.sparkline(self.pts([0, 1]), self.END)
        self.assertIn('viewBox="0 0 600 100"', s)
        self.assertIn('preserveAspectRatio="none"', s)
        self.assertIn('aria-hidden="true"', s)
        self.assertIn("vector-effect:non-scaling-stroke", s)
        self.assertNotIn("<text", s)

    def test_x_spans_60_minutes(self):
        d = paths(chart.sparkline(self.pts(range(60), lambda o: 1), self.END))[0]
        self.assertTrue(d.startswith("M0.0 2.0L10.2 2.0L"))
        self.assertTrue(d.endswith("L600.0 2.0"))

    def test_y_uses_nice_max_with_margins(self):
        # 最大 3 -> 上限 5。値 0 は y=98、値 3 は 98-96*3/5
        s = chart.sparkline(self.pts([1, 0], lambda o: 0 if o else 3), self.END)
        self.assertEqual(paths(s)[0], "M589.8 98.0L600.0 40.4")

    def test_all_zero_uses_0_to_1(self):
        s = chart.sparkline(self.pts([0], lambda o: 0), self.END)
        self.assertEqual(paths(s), ["M600.0 98.0L600.0 98.0"])

    def test_max_one_fills_to_top_margin(self):
        s = chart.sparkline(self.pts([0], lambda o: 1), self.END)
        self.assertEqual(paths(s), ["M600.0 2.0L600.0 2.0"])

    def test_gap_breaks_line(self):
        d = paths(chart.sparkline(self.pts([3, 2, 0], lambda o: 1), self.END))[0]
        self.assertEqual(d.count("M"), 2)

    def test_single_point_is_zero_length_round_segment(self):
        s = chart.sparkline(self.pts([5, 0], lambda o: 1), self.END)
        d = paths(s)[0]
        self.assertEqual(d.count("M"), 2)
        self.assertRegex(d, r"M(\S+) (\S+)L\1 \2")
        self.assertIn('stroke-linecap="round"', s)

    def test_seconds_are_floored_to_minute(self):
        s = chart.sparkline([(self.END.replace(second=30), 1)], self.END)
        self.assertIn("M600.0 ", paths(s)[0])


class DayChartTest(unittest.TestCase):
    def mk(self, pts, unit="ratio", warn=None, crit=None, end=at(12)):
        return chart.day_chart(pts, D, end, unit, warn, crit, "CPU")

    def minutes(self, a, b, v):
        return [(D + timedelta(minutes=m), v) for m in range(a, b)]

    def test_empty(self):
        s = self.mk([])
        self.assertIn(label("fest.detail.chart_empty"), s)
        self.assertNotIn("<svg", s)

    def test_frame_and_aria(self):
        s = self.mk(self.minutes(0, 10, 0.5))
        self.assertIn('viewBox="0 0 528 220"', s)
        self.assertIn('role="img"', s)
        self.assertIn(f'aria-label="{label("fest.detail.chart_aria", label="CPU")}"', s)

    def test_aria_label_is_escaped(self):
        s = chart.day_chart(self.minutes(0, 2, 1), D, at(1), "count", None, None, '<b>"x"')
        self.assertNotIn("<b>", s)

    def test_axis_labels_include_threshold_in_max(self):
        s = self.mk(self.minutes(0, 10, 0.5), crit=0.95)
        for t in (">0%<", ">50%<", ">100%<"):
            self.assertIn(t, s)

    def test_no_threshold_max_from_values(self):
        s = self.mk(self.minutes(0, 10, 3), unit="count")
        for t in (">0<", ">2.5<", ">5<"):
            self.assertIn(t, s)

    def test_range_maps_to_plot_area(self):
        pts = [(D, 1), (D + timedelta(minutes=1), 1), (at(12), 1)]
        self.assertEqual(paths(self.mk(pts, unit="count"))[0], "M56.0 8.0L56.6 8.0M456.0 8.0L456.0 8.0")

    def test_threshold_lines_and_texts(self):
        s = self.mk(self.minutes(0, 5, 0.1), warn=0.85, crit=0.95)
        self.assertIn('stroke-dasharray="6 4"', s)
        self.assertIn('stroke-dasharray="2 3"', s)
        self.assertIn("var(--warn)", s)
        self.assertIn("var(--crit)", s)
        self.assertIn(">注意 85%<", s)
        self.assertIn(">異常 95%<", s)

    def test_no_threshold_no_dashes(self):
        self.assertNotIn("stroke-dasharray", self.mk(self.minutes(0, 5, 0.1)))

    def _text_y(self, s, text):
        m = re.search(r'<text x="[^"]*" y="([^"]*)"[^>]*>' + text + "<", s)
        return float(m.group(1))

    def test_close_threshold_texts_are_shifted_up(self):
        far = self.mk(self.minutes(0, 5, 0.1), warn=0.85, crit=0.95)
        self.assertAlmostEqual(self._text_y(far, "異常 95%"), 8 + 188 * 0.05 + 4, places=1)
        mid = self.mk(self.minutes(0, 5, 0.1), warn=0.50, crit=0.55)
        self.assertAlmostEqual(self._text_y(mid, "異常 55%"), 8 + 188 * 0.45 + 4 - 14, places=1)

    def test_shifted_text_stays_inside_viewbox(self):
        s = self.mk(self.minutes(0, 5, 0.1), warn=0.90, crit=0.95)
        up, low = self._text_y(s, "異常 95%"), self._text_y(s, "注意 90%")
        self.assertGreaterEqual(up, 12)
        self.assertGreaterEqual(low - up, 14)

    def test_gap_breaks_line(self):
        pts = self.minutes(0, 3, 1) + self.minutes(10, 13, 1)
        self.assertEqual(paths(self.mk(pts, unit="count"))[0].count("M"), 2)

    def test_single_point_round_segment(self):
        s = self.mk(self.minutes(0, 3, 1) + self.minutes(10, 11, 1), unit="count")
        d = paths(s)[0]
        self.assertRegex(d, r"M(\S+) (\S+)L\1 \2$")
        self.assertIn('stroke-linecap="round"', s)

    def test_points_outside_range_ignored(self):
        self.assertIn(label("fest.detail.chart_empty"), self.mk([(at(13), 1)]))

    def hours(self, end):
        s = self.mk(self.minutes(0, 5, 1), unit="count", end=end)
        return re.findall(r">(\d\d):00<", s)

    def test_tick_interval_boundaries(self):
        self.assertEqual(self.hours(at(8)), [f"{h:02d}" for h in range(0, 9)])
        self.assertEqual(self.hours(at(9)), ["00", "02", "04", "06", "08"])
        self.assertEqual(self.hours(at(16)), [f"{h:02d}" for h in range(0, 17, 2)])
        self.assertEqual(self.hours(at(17)), ["00", "03", "06", "09", "12", "15"])


class FormatAxisTest(unittest.TestCase):
    def test_units(self):
        f = chart.format_axis
        self.assertEqual(f(0.85, "ratio"), "85%")
        self.assertEqual(f(10, "count"), "10")
        self.assertEqual(f(1500, "ms"), "1500 ms")
        self.assertEqual(f(120, "seconds"), "120 秒")
        self.assertEqual(f(80, "rps"), "80/s")
        self.assertEqual(f(2.5, "count"), "2.5")


if __name__ == "__main__":
    unittest.main()
