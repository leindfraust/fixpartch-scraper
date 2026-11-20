import unittest
import fixpart


class OrderingTests(unittest.TestCase):
    def test_popular_brands_first_then_the_rest(self):
        brands = ["zebra", "bosch", "acec"]
        categories = ["filter", "akku"]
        pending = fixpart.order_pending(brands, categories, {"bosch"}, set())
        self.assertEqual(pending[:2], [("bosch", "filter"), ("bosch", "akku")])
        self.assertEqual(pending[2:], [("zebra", "filter"), ("zebra", "akku"), ("acec", "filter"), ("acec", "akku")])

    def test_completed_pairs_are_excluded(self):
        pending = fixpart.order_pending(
            ["bosch"], ["filter", "akku"], {"bosch"}, {("bosch", "filter")},
        )
        self.assertEqual(pending, [("bosch", "akku")])

    def test_empty_popular_still_covers_every_pair(self):
        pending = fixpart.order_pending(["a", "b"], ["x"], set(), set())
        self.assertCountEqual(pending, [("a", "x"), ("b", "x")])


if __name__ == "__main__":
    unittest.main()
