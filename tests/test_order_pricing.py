from __future__ import annotations

import sys
import types
import unittest

# paper_trading imports db -> psycopg at import time; stub it for unit tests.
if "psycopg" not in sys.modules:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_stub.OperationalError = Exception
    psycopg_stub.connect = lambda *args, **kwargs: None
    psycopg_stub.types = types.SimpleNamespace(
        json=types.SimpleNamespace(Jsonb=lambda value: value)
    )
    sys.modules["psycopg"] = psycopg_stub

from kalshi_pipeline.paper_trading import _maker_price_for_side


class OrderPricingTests(unittest.TestCase):
    def test_maker_price_normal_spread(self) -> None:
        price = _maker_price_for_side(
            side="yes",
            book={"yes_bid": 40, "yes_ask": 45, "no_bid": 55, "no_ask": 60},
            maker_only=True,
            min_price_cents=1,
            max_price_cents=99,
        )
        self.assertEqual(price, 41)

    def test_maker_price_locked_spread(self) -> None:
        price = _maker_price_for_side(
            side="yes",
            book={"yes_bid": 40, "yes_ask": 41, "no_bid": 59, "no_ask": 60},
            maker_only=True,
            min_price_cents=1,
            max_price_cents=99,
        )
        self.assertEqual(price, 40)

    def test_maker_price_no_bids(self) -> None:
        price = _maker_price_for_side(
            side="yes",
            book={"yes_bid": None, "yes_ask": 55, "no_bid": 45, "no_ask": None},
            maker_only=True,
            min_price_cents=1,
            max_price_cents=99,
        )
        self.assertIsNone(price)

    def test_maker_price_wide_spread(self) -> None:
        price = _maker_price_for_side(
            side="yes",
            book={"yes_bid": 20, "yes_ask": 50, "no_bid": 50, "no_ask": 80},
            maker_only=True,
            min_price_cents=1,
            max_price_cents=99,
        )
        self.assertEqual(price, 21)


if __name__ == "__main__":
    unittest.main()
