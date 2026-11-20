import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import fixpart


def listing(sku="one", *, pages=1):
    product = {
        "sku": sku, "name": "Türfach", "url": "https://fixpart.ch/de/produkt/test",
        "brand": {"name": "Test"}, "offers": {"price": 12.50, "priceCurrency": "CHF"},
    }
    data = {"@type": "ItemList", "itemListElement": [{"item": product}]}
    return (
        '<script type="application/ld+json">' + json.dumps(data) + "</script>"
        f'<a data-page="{pages}">Last</a>'
    )


EMPTY_LISTING = """
<div class="productlist__wrapper">
  <h2 class="listing__heading">Bertazzoni 3D-Brille Produkte</h2>
  <div class="filter-btn" id="filters"><span>Filter 0 Ergebnisse</span></div>
  <div class="productlist__customerservice"><p>Sie können Ihr Produkt nicht finden?</p></div>
</div>
"""


GATE_PAGE = """
<div class="fp-modal fp-modal--no-close">
  <form action="?" method="post" id="confirmIsHuman">
    <p>Es wurde eine ungewöhnliche Aktivität Ihres Browsers festgestellt.</p>
    <div class="g-recaptcha" data-sitekey="test" data-size="invisible"></div>
    <button class="confirm-box-is-human btn-green" name="submitButton">
      Bestätigen Sie, dass Sie ein Mensch sind
    </button>
  </form>
</div>
"""


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_all_advertised_letter_pages(self):
        pages = {
            "/de/produktkategorien": '<a href="/de/ersatzteile/filter">Filter</a>',
            "/de/marken": (
                '<a href="https://fixpart.ch/de/ersatzteile/aeg">AEG</a>'
                '<a href="/de/marken?letter=B&amp;#letter">B</a>'
            ),
            "/de/marken?letter=B&": '<a href="/de/ersatzteile/beko">Beko</a>',
        }
        def handler(request):
            key = request.url.path
            if request.url.query:
                key += "?" + request.url.query.decode()
            return httpx.Response(200, text=pages[key])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            brands, categories, _ = await fixpart.discover(client)
        self.assertEqual(brands, ["aeg", "beko"])
        self.assertEqual(categories, ["filter"])

    async def test_discovery_error_is_not_empty_success(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        ) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await fixpart.discover(client)

    async def test_discovery_exposes_popular_brands(self):
        pages = {
            "/de/produktkategorien": '<a href="/de/ersatzteile/filter">Filter</a>',
            "/de/marken": (
                '<a class="blocklist__item blocklist__item--brand" href="/de/ersatzteile/aeg">AEG</a>'
                '<a href="?letter=B">B</a>'
            ),
            "/de/marken?letter=B": '<a href="/de/ersatzteile/beko">Beko</a>',
        }
        def handler(request):
            key = request.url.path
            if request.url.query:
                key += "?" + request.url.query.decode()
            return httpx.Response(200, text=pages[key])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            brands, categories, popular = await fixpart.discover(client)
        self.assertEqual(brands, ["aeg", "beko"])
        self.assertEqual(popular, {"aeg"})

    async def test_query_and_document_relative_links_use_document_url(self):
        pages = {
            "/de/produktkategorien": '<a href="ersatzteile/filter">Filter</a>',
            "/de/marken": (
                '<a href="ersatzteile/aeg">AEG</a>'
                '<a href="?letter=B">B</a>'
            ),
            "/de/marken?letter=B": '<a href="ersatzteile/beko">Beko</a>',
        }
        def handler(request):
            key = request.url.path
            if request.url.query:
                key += "?" + request.url.query.decode()
            return httpx.Response(200, text=pages[key])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            brands, categories, _ = await fixpart.discover(client)
        self.assertEqual(brands, ["aeg", "beko"])
        self.assertEqual(categories, ["filter"])


class ExtractionTests(unittest.TestCase):
    def test_explicit_zero_result_listing_without_itemlist_is_empty(self):
        self.assertEqual(fixpart.extract_items(EMPTY_LISTING), ([], 1))

    def test_zero_result_text_outside_listing_is_not_empty_evidence(self):
        with self.assertRaises(ValueError):
            fixpart.extract_items("<div>Filter 0 Ergebnisse</div>")

    def test_zero_result_counter_with_product_cards_is_not_empty(self):
        html = EMPTY_LISTING.replace(
            '<div class="filter-btn"', '<div class="productlist__item">Product</div><div class="filter-btn"',
        )
        with self.assertRaises(ValueError):
            fixpart.extract_items(html)

    def test_missing_itemlist_raises(self):
        with self.assertRaises(ValueError):
            fixpart.extract_items("<html>Access denied</html>")

    def test_served_human_confirmation_page_stops_the_run(self):
        with self.assertRaises(RuntimeError):
            fixpart.extract_items(GATE_PAGE)

    def test_missing_sku_raises(self):
        with self.assertRaises(ValueError):
            fixpart.extract_items(listing(""))

    def test_reads_reordered_script_attributes(self):
        html = listing().replace(
            '<script type="application/ld+json">',
            "<script id='catalogue' type='application/ld+json'>",
        )
        rows, pages = fixpart.extract_items(html)
        self.assertEqual(rows[0]["title"], "Türfach")
        self.assertEqual(pages, 1)

    def test_invalid_price_raises(self):
        for value in ("NaN", "-1", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fixpart.extract_items(listing().replace("12.5", json.dumps(value)))


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = fixpart.open_db(":memory:")
        self.state = {
            "db": self.db, "rows": 0, "pages": 0, "lock": asyncio.Lock(),
            "start": 0, "last_report": 0,
        }
        self.args = SimpleNamespace(max_pages=0, report=10000, concurrency=2)
        self.guard = fixpart.Guard(1.6)

    def tearDown(self):
        self.db.close()

    async def crawl(self, handler):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(self.guard, "wait", return_value=None):
                await fixpart.scrape_combo(
                    client, "beko", "filter", self.guard, self.state, self.args,
                )

    def progress(self):
        return self.db.execute(
            "SELECT next_page, done FROM progress WHERE brand='beko' AND category='filter'"
        ).fetchone()

    async def test_server_error_never_marks_done(self):
        try:
            await self.crawl(lambda r: httpx.Response(503))
        except httpx.HTTPStatusError:
            pass
        self.assertFalse(self.progress() and self.progress()[1])

    async def test_page_cap_keeps_cursor_pending_and_restart_fetches_next_page(self):
        self.args.max_pages = 1
        await self.crawl(lambda r: httpx.Response(200, text=listing(pages=2)))
        self.assertEqual(self.progress(), (2, 0))
        self.args.max_pages = 0
        def handler(request):
            self.assertEqual(request.url.params["page"], "2")
            return httpx.Response(200, text=listing("two", pages=2))
        await self.crawl(handler)
        self.assertEqual(self.progress(), (3, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 2)

    async def test_cancellation_does_not_mark_done(self):
        async def handler(request):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.crawl(handler)
        self.assertIsNone(self.progress())

    async def test_repeated_page_is_failure_not_completion(self):
        with self.assertRaises(ValueError):
            await self.crawl(lambda r: httpx.Response(200, text=listing(pages=2)))
        self.assertEqual(self.progress(), (2, 0))

    async def test_missing_pagination_on_later_page_stays_pending(self):
        def handler(request):
            page = request.url.params["page"]
            html = listing("one" if page == "1" else "two", pages=3)
            if page == "2":
                html = html.replace('<a data-page="3">Last</a>', "")
            return httpx.Response(200, text=html)
        with self.assertRaises(ValueError):
            await self.crawl(handler)
        self.assertEqual(self.progress(), (2, 0))

    async def test_saved_final_page_survives_restart_with_missing_pagination(self):
        self.args.max_pages = 1
        await self.crawl(lambda r: httpx.Response(200, text=listing(pages=3)))
        self.args.max_pages = 0
        with self.assertRaises(ValueError):
            await self.crawl(lambda r: httpx.Response(
                200, text=listing("two").replace('<a data-page="1">Last</a>', ""),
            ))
        self.assertEqual(self.progress(), (2, 0))

    async def test_network_failure_keeps_page_checkpoint(self):
        def handler(request):
            if request.url.params["page"] == "1":
                return httpx.Response(200, text=listing(pages=2))
            raise httpx.ReadError("Disconnected", request=request)
        await self.crawl(handler)
        self.assertEqual(self.progress(), (2, 0))

    async def test_missing_itemlist_stays_pending(self):
        with self.assertRaises(ValueError):
            await self.crawl(lambda r: httpx.Response(200, text="Maintenance"))
        self.assertIsNone(self.progress())

    async def test_verified_empty_first_page_is_complete(self):
        await self.crawl(lambda r: httpx.Response(200, text=EMPTY_LISTING))
        self.assertEqual(self.progress(), (1, 1))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)

    async def test_zero_results_on_later_page_remain_pending(self):
        def handler(request):
            html = listing(pages=3) if request.url.params["page"] == "1" else EMPTY_LISTING
            return httpx.Response(200, text=html)
        with self.assertRaises(ValueError):
            await self.crawl(handler)
        self.assertEqual(self.progress(), (2, 0))

    async def test_known_parent_redirect_marks_absent_listing(self):
        await self.crawl(lambda r: httpx.Response(
            302, headers={"location": "/de/ersatzteile/beko"},
        ))
        self.assertEqual(self.progress(), (1, 1))

    async def test_unknown_redirect_stays_pending(self):
        with self.assertRaises(httpx.HTTPStatusError):
            await self.crawl(lambda r: httpx.Response(
                302, headers={"location": "/de"},
            ))
        self.assertIsNone(self.progress())

    async def test_captcha_stops_without_marking_done(self):
        with self.assertRaises(RuntimeError):
            await self.crawl(lambda r: httpx.Response(
                302, headers={"location": "/de/captcha?redirect=somewhere"},
            ))
        self.assertIsNone(self.progress())

    async def test_served_captcha_page_stops_without_marking_done(self):
        with self.assertRaises(RuntimeError):
            await self.crawl(lambda r: httpx.Response(200, text=GATE_PAGE))
        self.assertIsNone(self.progress())

    async def test_untrusted_legacy_done_flag_does_not_skip_work(self):
        self.db.execute("CREATE TABLE combos (brand TEXT, category TEXT, done INTEGER)")
        self.db.execute("INSERT INTO combos VALUES ('beko', 'filter', 1)")
        self.db.commit()
        await self.crawl(lambda r: httpx.Response(200, text=listing()))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
        self.assertEqual(self.progress(), (2, 1))

    async def test_checkpoint_failure_rolls_back_products(self):
        self.db.execute(
            "CREATE TRIGGER reject_checkpoint BEFORE INSERT ON progress "
            "BEGIN SELECT RAISE(ABORT, 'test checkpoint failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.crawl(lambda r: httpx.Response(200, text=listing()))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM products").fetchone()[0], 0)
        self.assertIsNone(self.progress())

    async def test_worker_pool_bounds_active_listing_tasks(self):
        self.args.concurrency = 2
        active = 0
        peak = 0
        completed = []
        async def fake_crawl(client, brand, category, guard, state, args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            completed.append((brand, category))
        pending = [(str(i), "filter") for i in range(10)]
        with patch("fixpart.scrape_combo", side_effect=fake_crawl):
            await fixpart.crawl_pending(
                None, pending, guard=self.guard, state=self.state, args=self.args,
            )
        self.assertEqual(peak, 2)
        self.assertCountEqual(completed, pending)

    async def test_isolated_gated_listing_does_not_stop_the_batch(self):
        async def fake_crawl(client, brand, category, guard, state, args):
            if brand == "gated":
                raise RuntimeError("Captcha required; run stopped with progress preserved")
        pending = [("gated", "filter"), ("beko", "filter")]
        with patch("fixpart.scrape_combo", side_effect=fake_crawl):
            await fixpart.crawl_pending(
                None, pending, guard=self.guard, state=self.state, args=self.args,
            )

    async def test_unbroken_gate_aborts_the_batch(self):
        async def fake_crawl(client, brand, category, guard, state, args):
            raise RuntimeError("Captcha required; run stopped with progress preserved")
        pending = [("a", "filter")] * (fixpart.MAX_CONSECUTIVE_GATES + 1)
        with patch("fixpart.scrape_combo", side_effect=fake_crawl):
            with self.assertRaises(ExceptionGroup) as raised:
                await fixpart.crawl_pending(
                    None, pending, guard=self.guard, state=self.state, args=self.args,
                )
        self.assertIsInstance(raised.exception.exceptions[0], RuntimeError)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiter_rechecks_new_cooldown_before_sending(self):
        clock = [0.0]
        sleeps = []
        guard = fixpart.Guard(1)
        async def sleep(delay):
            sleeps.append(delay)
            clock[0] += delay
            if len(sleeps) == 1:
                guard.block(30)
        with patch("fixpart.time.monotonic", side_effect=lambda: clock[0]), patch(
            "fixpart.asyncio.sleep", side_effect=sleep,
        ):
            await guard.wait()
            await guard.wait()
        self.assertGreaterEqual(clock[0], 31)

    async def test_retry_after_is_respected(self):
        guard = fixpart.Guard(1)
        responses = iter([
            httpx.Response(429, headers={"retry-after": "42"}),
            httpx.Response(200),
        ])
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: next(responses)),
        ) as client:
            with patch.object(guard, "wait", return_value=None), patch.object(guard, "block") as block:
                result = await fixpart.get(client, "https://fixpart.ch/de/test", guard, {})
        self.assertEqual(result.status_code, 200)
        block.assert_called_once_with(42)


class StorageTests(unittest.TestCase):
    def test_existing_price_updates_without_counting_duplicate_as_new(self):
        db = fixpart.open_db(":memory:")
        self.addCleanup(db.close)
        row = fixpart.extract_items(listing())[0][0]
        self.assertEqual(fixpart.save_rows(db, [row]), 1)
        row["price"] = "18.99"
        self.assertEqual(fixpart.save_rows(db, [row]), 0)
        self.assertEqual(db.execute("SELECT price FROM products").fetchone()[0], "18.99")


if __name__ == "__main__":
    unittest.main()
