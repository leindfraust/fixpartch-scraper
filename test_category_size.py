import unittest

import fixpart


class CategorySizeTests(unittest.TestCase):
    def test_parses_category_claim(self):
        html = "Wir können mehr als 110’176 Produkte aus der Kategorie Dichtung liefern"
        self.assertEqual(fixpart.parse_brand_size(html), 110176)

    def test_missing_claim_returns_none(self):
        self.assertIsNone(fixpart.parse_brand_size("<html>no claim</html>"))


class CategoryOrderTests(unittest.TestCase):
    def test_dense_categories_come_first_within_a_brand(self):
        categories = ["akku", "dichtung", "filter"]
        cat_sizes = {"dichtung": 110176, "filter": 60000, "akku": 900}
        pending = fixpart.order_pending(
            ["bosch"], categories, set(), set(),
            sizes={"bosch": 10}, cat_sizes=cat_sizes,
        )
        self.assertEqual([c for _, c in pending], ["dichtung", "filter", "akku"])

    def test_brand_order_still_wins_over_category_order(self):
        pending = fixpart.order_pending(
            ["small", "big"], ["dichtung", "akku"], set(), set(),
            sizes={"big": 5000, "small": 1},
            cat_sizes={"dichtung": 100, "akku": 1},
        )
        self.assertEqual([b for b, _ in pending], ["big", "big", "small", "small"])


class CategorySizeCacheTests(unittest.TestCase):
    def test_category_sizes_round_trip(self):
        db = fixpart.open_db(":memory:")
        self.addCleanup(db.close)
        fixpart.save_category_size(db, "dichtung", 110176)
        self.assertEqual(fixpart.read_category_sizes(db), {"dichtung": 110176})


if __name__ == "__main__":
    unittest.main()
