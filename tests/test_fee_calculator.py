from __future__ import annotations

import unittest

from kalshi_pipeline.signals.bracket_arb import KalshiFeeCalculator


class FeeCalculatorTests(unittest.TestCase):
    def test_taker_fee_at_50_cents(self) -> None:
        fee = KalshiFeeCalculator.taker_fee(50)
        self.assertEqual(fee, 2)

    def test_taker_fee_at_5_cents(self) -> None:
        fee = KalshiFeeCalculator.taker_fee(5)
        self.assertGreaterEqual(fee, 1)

    def test_taker_fee_at_95_cents(self) -> None:
        fee = KalshiFeeCalculator.taker_fee(95)
        self.assertGreaterEqual(fee, 1)

    def test_taker_fee_rounds_up(self) -> None:
        fee = KalshiFeeCalculator.taker_fee(40)
        self.assertEqual(fee, int(fee))
        self.assertGreaterEqual(fee, 1)

    def test_maker_fee_always_zero(self) -> None:
        self.assertEqual(KalshiFeeCalculator.maker_fee(1), 0)
        self.assertEqual(KalshiFeeCalculator.maker_fee(50), 0)
        self.assertEqual(KalshiFeeCalculator.maker_fee(99), 0)


if __name__ == "__main__":
    unittest.main()
