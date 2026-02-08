from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient
from .models import MarketSnapshot, PaperTradeOrder, SignalRecord
from .risk import compute_order_size

logger = logging.getLogger(__name__)


def _price_to_cents(price: float | None) -> int | None:
    if price is None:
        return None
    if price > 1.0:
        return int(round(price))
    return int(round(price * 100))


def _extract_order_id(payload: dict[str, Any]) -> str | None:
    candidates = [payload.get("order_id"), payload.get("id")]
    order_obj = payload.get("order")
    if isinstance(order_obj, dict):
        candidates.extend([order_obj.get("order_id"), order_obj.get("id")])
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text
    return None


def _is_actionable(signal: SignalRecord) -> bool:
    return signal.direction in {"buy_yes", "buy_no"} and signal.market_ticker is not None


def _best_book_prices(snapshot: MarketSnapshot) -> dict[str, int | None]:
    raw = snapshot.raw_json if isinstance(snapshot.raw_json, dict) else {}
    yes_bid = _price_to_cents(raw.get("yes_bid"))
    yes_ask = _price_to_cents(raw.get("yes_ask"))
    no_bid = _price_to_cents(raw.get("no_bid"))
    no_ask = _price_to_cents(raw.get("no_ask"))
    if yes_bid is None:
        yes_bid = _price_to_cents(snapshot.yes_price)
    if no_bid is None:
        no_bid = _price_to_cents(snapshot.no_price)
    if yes_ask is None and no_bid is not None:
        yes_ask = 100 - no_bid
    if no_ask is None and yes_bid is not None:
        no_ask = 100 - yes_bid
    if no_bid is None and yes_ask is not None:
        no_bid = 100 - yes_ask
    if yes_bid is None and no_ask is not None:
        yes_bid = 100 - no_ask
    return {"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask}


def _maker_price_for_side(
    *,
    side: str,
    book: dict[str, int | None],
    maker_only: bool,
    min_price_cents: int,
    max_price_cents: int,
) -> int | None:
    if side == "yes":
        bid = book.get("yes_bid")
        ask = book.get("yes_ask")
    else:
        bid = book.get("no_bid")
        ask = book.get("no_ask")
    if bid is None and ask is None:
        return None
    if not maker_only:
        raw_price = ask if ask is not None else bid
        if raw_price is None:
            return None
        return max(min_price_cents, min(max_price_cents, raw_price))

    # Maker-only: never cross the ask. Keep price at or below the resting bid.
    if bid is None:
        return None
    if ask is None:
        return max(min_price_cents, min(max_price_cents, bid))

    if ask <= bid:
        maker_ceiling = bid
    elif ask - bid <= 1:
        maker_ceiling = bid
    else:
        maker_ceiling = ask - 1

    preferred = min(bid + 1, maker_ceiling)
    clamped = max(min_price_cents, min(max_price_cents, preferred))
    if clamped > maker_ceiling:
        return None
    return clamped


def _find_arbitrage(book: dict[str, int | None]) -> dict[str, int] | None:
    yes_ask = book.get("yes_ask")
    no_ask = book.get("no_ask")
    if yes_ask is None or no_ask is None:
        return None
    total_cost = yes_ask + no_ask
    if total_cost >= 100:
        return None
    return {
        "yes_price": yes_ask,
        "no_price": no_ask,
        "profit_per_contract": 100 - total_cost,
    }


class PaperTradingEngine:
    def __init__(self, settings: Settings, client: KalshiClient, store: PostgresStore) -> None:
        self.settings = settings
        self.client = client
        self.store = store

    def _submit_order(
        self,
        *,
        market_ticker: str,
        signal_type: str,
        direction: str,
        side: str,
        count: int,
        price_cents: int,
        now_utc: datetime,
    ) -> PaperTradeOrder:
        request_payload: dict[str, Any] = {
            "ticker": market_ticker,
            "side": side,
            "count": count,
            "price_cents": price_cents,
        }
        response_payload: dict[str, Any] = {}
        status = "simulated"
        reason: str | None = None
        external_order_id: str | None = None
        if self.settings.paper_trading_mode == "kalshi_demo":
            try:
                response_payload = self.client.place_order(
                    ticker=market_ticker,
                    side=side,
                    count=count,
                    price_cents=price_cents,
                    base_url=self.settings.paper_trading_base_url,
                )
                external_order_id = _extract_order_id(response_payload)
                status = "submitted"
            except Exception as exc:
                status = "failed"
                reason = str(exc)
                logger.exception(
                    "paper_trade_submit_failed ticker=%s side=%s reason=%s",
                    market_ticker,
                    side,
                    reason,
                )
        else:
            reason = "simulation_only"

        return PaperTradeOrder(
            market_ticker=market_ticker,
            signal_type=signal_type,
            direction=direction,
            side=side,
            count=count,
            limit_price_cents=price_cents,
            provider=self.settings.paper_trading_mode,
            status=status,
            reason=reason,
            external_order_id=external_order_id,
            request_payload=request_payload,
            response_payload=response_payload,
            created_at=now_utc,
        )

    def execute(
        self,
        signals: list[SignalRecord],
        snapshots_by_ticker: dict[str, MarketSnapshot],
        now_utc: datetime,
    ) -> tuple[list[PaperTradeOrder], dict[str, int]]:
        stats = {
            "paper_orders_candidates": 0,
            "paper_orders_attempted": 0,
            "paper_orders_submitted": 0,
            "paper_orders_simulated": 0,
            "paper_orders_failed": 0,
            "paper_orders_skipped": 0,
            "paper_orders_recorded": 0,
        }
        if not self.settings.paper_trading_enabled:
            return [], stats

        candidates = []
        for signal in signals:
            if not _is_actionable(signal):
                continue
            if signal.signal_type not in self.settings.paper_trade_signal_types:
                continue
            if abs(signal.edge_bps or 0.0) < self.settings.paper_trade_min_edge_bps:
                continue
            confidence = signal.confidence if signal.confidence is not None else 0.0
            if confidence < self.settings.paper_trade_min_confidence:
                continue
            candidates.append(signal)
        candidates.sort(key=lambda signal: abs(signal.edge_bps or 0.0), reverse=True)
        stats["paper_orders_candidates"] = len(candidates)

        open_positions = self.store.get_open_positions_summary()
        current_exposure_dollars = sum(
            position["contracts"] * (position["avg_price_cents"] / 100.0)
            for position in open_positions
        )

        orders: list[PaperTradeOrder] = []
        cooldown_since = now_utc - timedelta(minutes=self.settings.paper_trade_cooldown_minutes)
        max_orders = self.settings.paper_trade_max_orders_per_cycle

        # Arbitrage gets first priority if enabled.
        if self.settings.paper_trade_enable_arbitrage:
            seen_tickers: set[str] = set()
            for signal in candidates:
                ticker = signal.market_ticker
                if ticker is None or ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)
                if stats["paper_orders_attempted"] + 2 > max_orders:
                    break
                snapshot = snapshots_by_ticker.get(ticker)
                if snapshot is None:
                    continue
                book = _best_book_prices(snapshot)
                arb = _find_arbitrage(book)
                if arb is None:
                    continue
                for side_key, side_name in (("yes_price", "yes"), ("no_price", "no")):
                    stats["paper_orders_attempted"] += 1
                    order = self._submit_order(
                        market_ticker=ticker,
                        signal_type=signal.signal_type,
                        direction="arbitrage",
                        side=side_name,
                        count=self.settings.paper_trade_contract_count,
                        price_cents=int(arb[side_key]),
                        now_utc=now_utc,
                    )
                    orders.append(order)
                    if order.status == "submitted":
                        stats["paper_orders_submitted"] += 1
                        current_exposure_dollars += (
                            order.count * (order.limit_price_cents / 100.0)
                        )
                    elif order.status == "simulated":
                        stats["paper_orders_simulated"] += 1
                    else:
                        stats["paper_orders_failed"] += 1

        for signal in candidates:
            if stats["paper_orders_attempted"] >= max_orders:
                break
            ticker = signal.market_ticker
            if ticker is None:
                continue
            if self.store.has_recent_paper_order(ticker, signal.direction, cooldown_since):
                stats["paper_orders_skipped"] += 1
                continue
            snapshot = snapshots_by_ticker.get(ticker)
            if snapshot is None:
                stats["paper_orders_skipped"] += 1
                continue

            side = "yes" if signal.direction == "buy_yes" else "no"
            book = _best_book_prices(snapshot)
            price_cents = _maker_price_for_side(
                side=side,
                book=book,
                maker_only=self.settings.paper_trade_maker_only,
                min_price_cents=self.settings.paper_trade_min_price_cents,
                max_price_cents=self.settings.paper_trade_max_price_cents,
            )
            if price_cents is None:
                stats["paper_orders_skipped"] += 1
                continue
            count = compute_order_size(
                signal=signal,
                side=side,
                market_price_cents=price_cents,
                settings=self.settings,
                current_exposure_dollars=current_exposure_dollars,
                bankroll_dollars=self.settings.paper_trade_max_portfolio_exposure_dollars,
            )
            if count <= 0:
                stats["paper_orders_skipped"] += 1
                continue

            stats["paper_orders_attempted"] += 1
            order = self._submit_order(
                market_ticker=ticker,
                signal_type=signal.signal_type,
                direction=signal.direction,
                side=side,
                count=count,
                price_cents=price_cents,
                now_utc=now_utc,
            )
            orders.append(order)
            if order.status == "submitted":
                stats["paper_orders_submitted"] += 1
                current_exposure_dollars += order.count * (order.limit_price_cents / 100.0)
            elif order.status == "simulated":
                stats["paper_orders_simulated"] += 1
            else:
                stats["paper_orders_failed"] += 1

        if orders:
            stats["paper_orders_recorded"] = self.store.insert_paper_trade_orders(orders)
        return orders, stats
