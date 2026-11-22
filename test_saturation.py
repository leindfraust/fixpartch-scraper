import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

import fixpart


def page(skus, pages=1):
    items = [
        {"item": {"sku": s, "name": f"T{s}", "url": f"u{s}",
                  "offers": {"price": 1, "priceCurrency": "CHF"}}}
        for s in skus
    ]
    data = {"@type": "ItemList", "itemListElement": items}
    return (
        '<script type="application/ld+json">' + json.dumps(data) + "</script>"
        f'<a data-page="{pages}">Last</a>'
    )


class SaturationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = fixpart.open_db(":memory:")
        self.state = {"db": self.db, "rows": 0, "pages": 0, "lock": asyncio.Lock(),
                      "start": 0, "last_report": 0}
        self.args = SimpleNamespace(max_pages=0, report=10000)
        self.guard = fixpart.Guard(1.6)
        fixpart.save_rows(self.db, fixpart.extract_items(page(["1", "2"]))[0])

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
            "SELECT next_page, done FROM progress WHERE brand='beko'"
        ).fetchone()

    async def test_duplicate_first_page_stops_without_reading_the_rest(self):
        seen = []
        def handler(request):
            seen.append(request.url.params["page"])
            return httpx.Response(200, text=page(["1", "2"], pages=40))
        await self.crawl(handler)
        self.assertEqual(seen, ["1"])
        self.assertEqual(self.progress(), (2, 1))

    async def test_two_consecutive_duplicate_pages_stop_mid_listing(self):
        seen = []
        def handler(request):
            p = request.url.params["page"]
            seen.append(p)
            skus = {"1": ["3"], "2": ["1"], "3": ["2"]}.get(p, ["x"])
            return httpx.Response(200, text=page(skus, pages=40))
        await self.crawl(handler)
        self.assertEqual(seen, ["1", "2", "3"])
        self.assertEqual(self.progress(), (4, 1))

    async def test_new_products_on_every_page_continue_to_the_end(self):
        seen = []
        def handler(request):
            p = request.url.params["page"]
            seen.append(p)
            return httpx.Response(200, text=page([f"n{p}"], pages=3))
        await self.crawl(handler)
        self.assertEqual(seen, ["1", "2", "3"])
        self.assertEqual(self.progress(), (4, 1))


if __name__ == "__main__":
    unittest.main()

