import os
import tempfile
import unittest

import fixpart

TAIL = (
    '<div class="productlist__wrapper">'
    '<h2 class="listing__heading">AEG Abdeckkappe Produkte</h2>'
    '<div class="filter-btn" id="filters"><span>Filter 740 Ergebnisse</span></div>'
    '</div><a data-page="30">30</a>'
)


class LockTests(unittest.TestCase):
    def test_live_holder_blocks_second_run(self):
        path = os.path.join(tempfile.mkdtemp(), "fixpart.lock")
        self.assertTrue(fixpart.acquire_lock(path, pid=os.getpid()))
        self.assertFalse(fixpart.acquire_lock(path, pid=os.getpid()))
        fixpart.release_lock(path, pid=os.getpid())

    def test_stale_lock_is_reclaimed(self):
        path = os.path.join(tempfile.mkdtemp(), "fixpart.lock")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("0")
        self.assertTrue(fixpart.acquire_lock(path, pid=os.getpid()))
        fixpart.release_lock(path, pid=os.getpid())


class TailPageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import asyncio
        from types import SimpleNamespace
        self.db = fixpart.open_db(":memory:")
        self.state = {"db": self.db, "rows": 0, "pages": 0, "lock": asyncio.Lock(), "start": 0, "last_report": 0}
        self.args = SimpleNamespace(max_pages=0, report=10000)
        self.guard = fixpart.Guard(1.6)

    def tearDown(self):
        self.db.close()

    async def crawl(self, handler):
        import asyncio, httpx
        from unittest.mock import patch
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(self.guard, "wait", return_value=None):
                await fixpart.scrape_combo(client, "aeg", "abdeckkappe", self.guard, self.state, self.args)

    def progress(self):
        return self.db.execute(
            "SELECT next_page,last_sku,done,max_page FROM progress WHERE brand='aeg'"
        ).fetchone()

    async def test_empty_tail_page_completes_the_listing(self):
        import httpx, json
        first = (
            '<script type="application/ld+json">'
            + json.dumps({"@type": "ItemList", "itemListElement": [{"item": {
                "sku": "1", "name": "T", "url": "u", "offers": {"price": 1, "priceCurrency": "CHF"}}}]})
            + '</script><a data-page="2">2</a>'
        )
        def handler(request):
            return httpx.Response(200, text=first if request.url.params["page"] == "1" else TAIL)
        await self.crawl(handler)
        self.assertEqual(self.progress(), (2, "1", 1, 2))

    async def test_empty_first_page_shell_stays_pending(self):
        import httpx
        with self.assertRaises(ValueError):
            await self.crawl(lambda r: httpx.Response(200, text=TAIL))
        self.assertIsNone(self.progress())


if __name__ == "__main__":
    unittest.main()
