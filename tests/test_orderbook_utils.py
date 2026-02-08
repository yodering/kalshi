from __future__ import annotations

import unittest

from kalshi_pipeline.orderbook_utils import (
    compute_vwap,
    effective_no_ask_vwap,
    effective_yes_ask_vwap,
)


class OrderbookUtilsTests(unittest.TestCase):
    def test_vwap_single_level(self) -> None:
        result = compute_vwap([(42, 10)], 5, ascending=True)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[0], 42.0)
        self.assertEqual(result[1], 5)

    def test_vwap_multiple_levels(self) -> None:
        result = compute_vwap([(40, 2), (41, 3), (45, 10)], 5, ascending=True)
        self.assertIsNotNone(result)
        assert result is not None
        # (40*2 + 41*3) / 5 = 40.6
        self.assertAlmostEqual(result[0], 40.6, places=6)
        self.assertEqual(result[1], 5)

    def test_vwap_insufficient_liquidity_returns_partial_fill(self) -> None:
        result = compute_vwap([(30, 2), (35, 1)], 10, ascending=True)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[1], 3)
        self.assertAlmostEqual(result[0], (30 * 2 + 35) / 3.0, places=6)

    def test_yes_ask_from_no_bids(self) -> None:
        orderbook = {"no": [(70, 2), (65, 3)]}
        result = effective_yes_ask_vwap(orderbook, 4)
        self.assertIsNotNone(result)
        assert result is not None
        # YES asks are 30 (qty2), 35 (qty3) -> fill 4 => (30*2 + 35*2)/4 = 32.5
        self.assertAlmostEqual(result[0], 32.5, places=6)
        self.assertEqual(result[1], 4)

    def test_no_ask_from_yes_bids(self) -> None:
        orderbook = {"yes": [(60, 1), (55, 4)]}
        result = effective_no_ask_vwap(orderbook, 3)
        self.assertIsNotNone(result)
        assert result is not None
        # NO asks are 40 (qty1), 45 (qty4) -> fill 3 => (40 + 45*2) / 3 = 43.333...
        self.assertAlmostEqual(result[0], (40 + 45 * 2) / 3.0, places=6)
        self.assertEqual(result[1], 3)


if __name__ == "__main__":
    unittest.main()
