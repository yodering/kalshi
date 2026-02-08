from __future__ import annotations

from datetime import datetime, timezone
import unittest

from kalshi_pipeline.signals.bracket_arb import scan_bracket_arbitrage


def _book(
    *,
    yes_bid: int | None = None,
    yes_depth: int = 10,
    no_bid: int | None = None,
    no_depth: int = 10,
) -> dict[str, list[tuple[int, int]]]:
    payload: dict[str, list[tuple[int, int]]] = {"yes": [], "no": []}
    if yes_bid is not None:
        payload["yes"] = [(yes_bid, yes_depth)]
    if no_bid is not None:
        payload["no"] = [(no_bid, no_depth)]
    return payload


class BracketArbTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 2, 8, 0, 0, tzinfo=timezone.utc)

    def test_all_yes_arbitrage_detected(self) -> None:
        tickers = ["KXHIGHNY-A", "KXHIGHNY-B"]
        orderbooks = {
            "KXHIGHNY-A": _book(no_bid=70, no_depth=15),
            "KXHIGHNY-B": _book(no_bid=68, no_depth=12),
        }
        opp = scan_bracket_arbitrage(
            event_ticker="KXHIGHNY-TEST",
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            now_utc=self.now,
        )
        self.assertIsNotNone(opp)
        assert opp is not None
        self.assertEqual(opp.arb_type, "all_yes")
        self.assertGreater(opp.profit_after_fees_cents, 0)

    def test_all_yes_no_arbitrage_when_sum_at_or_above_100(self) -> None:
        tickers = ["KXHIGHNY-A", "KXHIGHNY-B"]
        orderbooks = {
            "KXHIGHNY-A": _book(no_bid=49, no_depth=20),
            "KXHIGHNY-B": _book(no_bid=49, no_depth=20),
        }
        opp = scan_bracket_arbitrage(
            event_ticker="KXHIGHNY-TEST",
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            now_utc=self.now,
        )
        self.assertIsNone(opp)

    def test_all_no_arbitrage_detected(self) -> None:
        tickers = ["KXHIGHNY-A", "KXHIGHNY-B", "KXHIGHNY-C"]
        orderbooks = {
            "KXHIGHNY-A": _book(yes_bid=45, yes_depth=30),
            "KXHIGHNY-B": _book(yes_bid=44, yes_depth=20),
            "KXHIGHNY-C": _book(yes_bid=46, yes_depth=18),
        }
        opp = scan_bracket_arbitrage(
            event_ticker="KXHIGHNY-TEST",
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            now_utc=self.now,
        )
        self.assertIsNotNone(opp)
        assert opp is not None
        self.assertEqual(opp.arb_type, "all_no")
        self.assertGreater(opp.profit_after_fees_cents, 0)

    def test_fee_can_eliminate_small_arbitrage(self) -> None:
        tickers = ["KXHIGHNY-A", "KXHIGHNY-B"]
        orderbooks = {
            "KXHIGHNY-A": _book(no_bid=51, no_depth=15),
            "KXHIGHNY-B": _book(no_bid=51, no_depth=15),
        }
        opp = scan_bracket_arbitrage(
            event_ticker="KXHIGHNY-TEST",
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            now_utc=self.now,
        )
        self.assertIsNone(opp)

    def test_depth_limits_max_sets(self) -> None:
        tickers = ["KXHIGHNY-A", "KXHIGHNY-B", "KXHIGHNY-C"]
        orderbooks = {
            "KXHIGHNY-A": _book(yes_bid=45, yes_depth=50),
            "KXHIGHNY-B": _book(yes_bid=45, yes_depth=2),
            "KXHIGHNY-C": _book(yes_bid=45, yes_depth=60),
        }
        opp = scan_bracket_arbitrage(
            event_ticker="KXHIGHNY-TEST",
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            now_utc=self.now,
        )
        self.assertIsNotNone(opp)
        assert opp is not None
        self.assertEqual(opp.max_sets, 2)

    def test_missing_orderbook_returns_none(self) -> None:
        tickers = ["KXHIGHNY-A", "KXHIGHNY-B"]
        orderbooks = {"KXHIGHNY-A": _book(no_bid=70, no_depth=10)}
        opp = scan_bracket_arbitrage(
            event_ticker="KXHIGHNY-TEST",
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            now_utc=self.now,
        )
        self.assertIsNone(opp)

    def test_single_bracket_returns_none(self) -> None:
        opp = scan_bracket_arbitrage(
            event_ticker="KXHIGHNY-TEST",
            bracket_tickers=["KXHIGHNY-A"],
            orderbooks={"KXHIGHNY-A": _book(no_bid=70, no_depth=10)},
            now_utc=self.now,
        )
        self.assertIsNone(opp)


if __name__ == "__main__":
    unittest.main()
