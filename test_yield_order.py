import unittest

import fixpart


class ExpectedYieldOrderTests(unittest.TestCase):
    def test_dense_category_beats_space_category_even_for_small_brand(self):
        brands = ["big", "small"]
        cats = ["dense", "sparse"]
        pending = fixpart.order_pending(
            brands, cats, set(), set(),
            sizes={"big": 1000, "small": 10},
            cat_sizes={"dense": 500, "sparse": 1},
        )
        self.assertEqual(
            pending,
            [("big", "dense"), ("small", "dense"), ("big", "sparse"), ("small", "sparse")],
        )

    def test_largest_claim_wins_within_the_same_category(self):
        pending = fixpart.order_pending(
            ["small", "big"], ["dense"], set(), set(),
            sizes={"big": 1000, "small": 10},
            cat_sizes={"dense": 500},
        )
        self.assertEqual(pending, [("big", "dense"), ("small", "dense")])

    def test_done_combinations_stay_excluded(self):
        pending = fixpart.order_pending(
            ["big"], ["dense", "sparse"], set(), {("big", "dense")},
            sizes={"big": 100}, cat_sizes={"dense": 10, "sparse": 1},
        )
        self.assertEqual(pending, [("big", "sparse")])

    def test_without_category_sizes_it_falls_back_to_brand_order(self):
        pending = fixpart.order_pending(
            ["small", "big"], ["a", "b"], set(), set(), sizes={"big": 10, "small": 1},
        )
        self.assertEqual([b for b, _ in pending], ["big", "big", "small", "small"])


if __name__ == "__main__":
    unittest.main()
