from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class BracketArbOpportunity:
    detected_at: datetime
    event_ticker: str
    arb_type: str  # "all_yes" or "all_no"
    legs: list[dict[str, Any]]
    cost_cents: int
    payout_cents: int
    profit_cents: int
    max_sets: int
    total_profit_cents: int
    profit_after_fees_cents: int


class KalshiFeeCalculator:
    @staticmethod
    def taker_fee(price_cents: int) -> int:
        p = max(1, min(99, int(price_cents))) / 100.0
        fee_dollars = 0.07 * p * (1.0 - p)
        return max(1, int((fee_dollars * 100.0) + 0.999))

    @staticmethod
    def maker_fee(_price_cents: int) -> int:
        return 0


def _normalize_levels(raw_levels: Any) -> list[tuple[int, int]]:
    if not isinstance(raw_levels, list):
        return []
    output: list[tuple[int, int]] = []
    for row in raw_levels:
        price = None
        qty = None
        if isinstance(row, dict):
            price = row.get("price")
            qty = row.get("quantity", row.get("qty"))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price = row[0]
            qty = row[1]
        try:
            parsed_price = int(price)
            parsed_qty = int(qty)
        except (TypeError, ValueError):
            continue
        if parsed_qty <= 0:
            continue
        output.append((parsed_price, parsed_qty))
    output.sort(key=lambda row: row[0], reverse=True)
    return output


def _best_bid_and_depth(raw_levels: Any) -> tuple[int, int] | None:
    levels = _normalize_levels(raw_levels)
    if not levels:
        return None
    best_price = levels[0][0]
    depth = sum(qty for price, qty in levels if price == best_price)
    if depth <= 0:
        return None
    return best_price, depth


def _all_yes_candidate(
    *,
    event_ticker: str,
    bracket_tickers: list[str],
    orderbooks: dict[str, dict[str, Any]],
    fee_calculator: KalshiFeeCalculator,
    now_utc: datetime,
) -> BracketArbOpportunity | None:
    legs: list[dict[str, Any]] = []
    total_cost = 0
    min_depth: int | None = None
    total_fees = 0
    for ticker in bracket_tickers:
        orderbook = orderbooks.get(ticker)
        if not isinstance(orderbook, dict):
            return None
        best_no = _best_bid_and_depth(orderbook.get("no"))
        if best_no is None:
            return None
        no_bid, depth = best_no
        yes_ask = 100 - no_bid
        yes_ask = max(1, min(99, yes_ask))
        legs.append(
            {
                "ticker": ticker,
                "side": "yes",
                "price_cents": yes_ask,
                "depth": depth,
            }
        )
        total_cost += yes_ask
        total_fees += fee_calculator.taker_fee(yes_ask)
        min_depth = depth if min_depth is None else min(min_depth, depth)

    payout = 100
    if total_cost >= payout:
        return None
    max_sets = max(0, int(min_depth or 0))
    if max_sets <= 0:
        return None
    profit_cents = payout - total_cost
    profit_after_fees_per_set = profit_cents - total_fees
    if profit_after_fees_per_set <= 0:
        return None
    return BracketArbOpportunity(
        detected_at=now_utc,
        event_ticker=event_ticker,
        arb_type="all_yes",
        legs=legs,
        cost_cents=total_cost,
        payout_cents=payout,
        profit_cents=profit_cents,
        max_sets=max_sets,
        total_profit_cents=profit_cents * max_sets,
        profit_after_fees_cents=profit_after_fees_per_set * max_sets,
    )


def _all_no_candidate(
    *,
    event_ticker: str,
    bracket_tickers: list[str],
    orderbooks: dict[str, dict[str, Any]],
    fee_calculator: KalshiFeeCalculator,
    now_utc: datetime,
) -> BracketArbOpportunity | None:
    n_brackets = len(bracket_tickers)
    if n_brackets < 2:
        return None
    legs: list[dict[str, Any]] = []
    total_cost = 0
    min_depth: int | None = None
    total_fees = 0
    for ticker in bracket_tickers:
        orderbook = orderbooks.get(ticker)
        if not isinstance(orderbook, dict):
            return None
        best_yes = _best_bid_and_depth(orderbook.get("yes"))
        if best_yes is None:
            return None
        yes_bid, depth = best_yes
        no_ask = 100 - yes_bid
        no_ask = max(1, min(99, no_ask))
        legs.append(
            {
                "ticker": ticker,
                "side": "no",
                "price_cents": no_ask,
                "depth": depth,
            }
        )
        total_cost += no_ask
        total_fees += fee_calculator.taker_fee(no_ask)
        min_depth = depth if min_depth is None else min(min_depth, depth)

    payout = (n_brackets - 1) * 100
    if total_cost >= payout:
        return None
    max_sets = max(0, int(min_depth or 0))
    if max_sets <= 0:
        return None
    profit_cents = payout - total_cost
    profit_after_fees_per_set = profit_cents - total_fees
    if profit_after_fees_per_set <= 0:
        return None
    return BracketArbOpportunity(
        detected_at=now_utc,
        event_ticker=event_ticker,
        arb_type="all_no",
        legs=legs,
        cost_cents=total_cost,
        payout_cents=payout,
        profit_cents=profit_cents,
        max_sets=max_sets,
        total_profit_cents=profit_cents * max_sets,
        profit_after_fees_cents=profit_after_fees_per_set * max_sets,
    )


def scan_bracket_arbitrage(
    *,
    event_ticker: str,
    bracket_tickers: list[str],
    orderbooks: dict[str, dict[str, Any]],
    fee_calculator: KalshiFeeCalculator | None = None,
    min_profit_after_fees_cents: int = 0,
    now_utc: datetime | None = None,
) -> BracketArbOpportunity | None:
    tickers = [ticker.strip().upper() for ticker in bracket_tickers if ticker.strip()]
    if len(tickers) < 2:
        return None
    calculator = fee_calculator or KalshiFeeCalculator()
    detection_time = now_utc or datetime.now(timezone.utc)

    candidates = [
        _all_yes_candidate(
            event_ticker=event_ticker,
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            fee_calculator=calculator,
            now_utc=detection_time,
        ),
        _all_no_candidate(
            event_ticker=event_ticker,
            bracket_tickers=tickers,
            orderbooks=orderbooks,
            fee_calculator=calculator,
            now_utc=detection_time,
        ),
    ]
    valid = [
        candidate
        for candidate in candidates
        if candidate is not None and candidate.profit_after_fees_cents > min_profit_after_fees_cents
    ]
    if not valid:
        return None
    valid.sort(key=lambda row: row.profit_after_fees_cents, reverse=True)
    return valid[0]
