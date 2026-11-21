import unittest

import fixpart


class BrandSizeTests(unittest.TestCase):
    def test_parses_apostrophe_thousands(self):
        html = "Wir können mehr als 4’604 Produkte für Kluge Geräte liefern"
        self.assertEqual(fixpart.parse_brand_size(html), 4604)

    def test_parses_plain_and_dotted_numbers(self):
        self.assertEqual(fixpart.parse_brand_size("mehr als 109 Produkte"), 109)
        self.assertEqual(fixpart.parse_brand_size("mehr als 10.151 Produkte"), 10151)

    def test_missing_claim_returns_none(self):
        self.assertIsNone(fixpart.parse_brand_size("<html>Blocked</html>"))

    def test_orders_brands_by_size_descending(self):
        brands = ["small", "big", "mid"]
        sizes = {"big": 5000, "mid": 800, "small": 3}
        pending = fixpart.order_pending(brands, ["filter"], set(), set(), sizes=sizes)
        self.assertEqual([b for b, _ in pending], ["big", "mid", "small"])

    def test_unmeasured_brands_sort_last(self):
        sizes = {"big": 5000}
        pending = fixpart.order_pending(
            ["unknown", "big"], ["filter"], set(), set(), sizes=sizes,
        )
        self.assertEqual([b for b, _ in pending], ["big", "unknown"])


class BrandSizeCacheTests(unittest.TestCase):
    def test_sizes_are_cached_and_read_back(self):
        db = fixpart.open_db(":memory:")
        self.addCleanup(db.close)
        fixpart.save_brand_size(db, "bosch", 1204)
        fixpart.save_brand_size(db, "bosch", 1300)
        self.assertEqual(fixpart.read_brand_sizes(db), {"bosch": 1300})


if __name__ == "__main__":
    unittest.main()
