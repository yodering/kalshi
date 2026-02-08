from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from kalshi_pipeline.models import SignalRecord
from kalshi_pipeline.risk import compute_order_size, kelly_fraction


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        paper_trade_sizing_mode="kelly",
        paper_trade_contract_count=2,
        kelly_fraction_scale=0.25,
        paper_trade_max_position_dollars=50.0,
        paper_trade_max_portfolio_exposure_dollars=500.0,
        paper_trade_default_fill_probability=0.5,
    )


def _signal(model_probability: float, confidence: float = 1.0) -> SignalRecord:
    return SignalRecord(
        signal_type="btc",
        market_ticker="KXBTC15M-TEST",
        direction="buy_yes",
        model_probability=model_probability,
        market_probability=0.5,
        edge_bps=0.0,
        confidence=confidence,
        data_source="rest",
        vwap_cents=None,
        fillable_qty=None,
        liquidity_sufficient=None,
        details={},
        created_at=datetime.now(timezone.utc),
    )


class RiskTests(unittest.TestCase):
    def test_kelly_positive_edge(self) -> None:
        value = kelly_fraction(model_prob=0.6, market_price_cents=50, side="yes")
        self.assertGreater(value, 0.0)

    def test_kelly_negative_edge_returns_zero(self) -> None:
        value = kelly_fraction(model_prob=0.4, market_price_cents=50, side="yes")
        self.assertEqual(value, 0.0)

    def test_kelly_edge_near_zero(self) -> None:
        value = kelly_fraction(model_prob=0.5, market_price_cents=50, side="yes")
        self.assertEqual(value, 0.0)

    def test_sizing_respects_max_position(self) -> None:
        settings = _settings()
        settings.paper_trade_max_position_dollars = 10.0
        contracts = compute_order_size(
            signal=_signal(0.8),
            side="yes",
            market_price_cents=20,
            settings=settings,
            current_exposure_dollars=0.0,
            bankroll_dollars=500.0,
            fill_probability=1.0,
        )
        self.assertLessEqual(contracts, 50)  # 10 dollars / 0.20 per contract

    def test_sizing_respects_portfolio_cap(self) -> None:
        settings = _settings()
        settings.paper_trade_max_portfolio_exposure_dollars = 100.0
        contracts = compute_order_size(
            signal=_signal(0.8),
            side="yes",
            market_price_cents=50,
            settings=settings,
            current_exposure_dollars=97.0,
            bankroll_dollars=500.0,
            fill_probability=1.0,
        )
        self.assertLessEqual(contracts, 6)  # remaining dollars ~= 3

    def test_adjusted_kelly_with_fill_probability(self) -> None:
        settings = _settings()
        high_fill = compute_order_size(
            signal=_signal(0.75),
            side="yes",
            market_price_cents=40,
            settings=settings,
            current_exposure_dollars=0.0,
            bankroll_dollars=500.0,
            fill_probability=1.0,
        )
        low_fill = compute_order_size(
            signal=_signal(0.75),
            side="yes",
            market_price_cents=40,
            settings=settings,
            current_exposure_dollars=0.0,
            bankroll_dollars=500.0,
            fill_probability=0.2,
        )
        self.assertGreater(high_fill, low_fill)

    def test_sizing_with_zero_bankroll(self) -> None:
        settings = _settings()
        contracts = compute_order_size(
            signal=_signal(0.75),
            side="yes",
            market_price_cents=40,
            settings=settings,
            current_exposure_dollars=500.0,
            bankroll_dollars=0.0,
            fill_probability=1.0,
        )
        self.assertEqual(contracts, 0)


if __name__ == "__main__":
    unittest.main()
