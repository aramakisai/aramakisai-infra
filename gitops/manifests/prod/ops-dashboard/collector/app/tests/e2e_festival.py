"""当日監視画面の実ブラウザ E2E (手動実行)。collector/app で `python3 -I tests/e2e_festival.py`。

CI の unittest discover の対象外 (ファイル名が test_ で始まらない)。ローカルの collector と一時 SQLite を使い、
Playwright の headless chromium を 1920x1080 で動かす。自動更新は Playwright の clock で進める。
"""
import os
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
from model import FestivalItem, Status  # noqa: E402
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
    def put(cls, items):
        cls.store.replace_festival_items(items, datetime.now(timezone.utc))

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


if __name__ == "__main__":
    unittest.main()
