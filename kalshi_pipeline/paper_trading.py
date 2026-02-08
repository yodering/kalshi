from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient
from .models import MarketSnapshot, PaperTradeOrder, SignalRecord

logger = logging.getLogger(__name__)


def _price_to_cents(price: float | None) -> int | None:
    if price is None:
        return None
    if price > 1.0:
        return int(round(price))
    return int(round(price * 100))


def _extract_order_id(payload: dict[str, Any]) -> str | None:
    candidates = [
        payload.get("order_id"),
        payload.get("id"),
    ]
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


class PaperTradingEngine:
    def __init__(self, settings: Settings, client: KalshiClient, store: PostgresStore) -> None:
        self.settings = settings
        self.client = client
        self.store = store

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
            edge_bps = abs(signal.edge_bps or 0.0)
            if edge_bps < self.settings.paper_trade_min_edge_bps:
                continue
            confidence = signal.confidence if signal.confidence is not None else 0.0
            if confidence < self.settings.paper_trade_min_confidence:
                continue
            candidates.append(signal)
        candidates.sort(key=lambda signal: abs(signal.edge_bps or 0.0), reverse=True)
        stats["paper_orders_candidates"] = len(candidates)

        orders: list[PaperTradeOrder] = []
        cooldown_since = now_utc - timedelta(minutes=self.settings.paper_trade_cooldown_minutes)
        max_orders = self.settings.paper_trade_max_orders_per_cycle
        for signal in candidates:
            if stats["paper_orders_attempted"] >= max_orders:
                break
            if signal.market_ticker is None:
                continue
            if self.store.has_recent_paper_order(signal.market_ticker, signal.direction, cooldown_since):
                stats["paper_orders_skipped"] += 1
                continue

            snapshot = snapshots_by_ticker.get(signal.market_ticker)
            if snapshot is None:
                stats["paper_orders_skipped"] += 1
                continue

            side = "yes" if signal.direction == "buy_yes" else "no"
            raw_price = snapshot.yes_price if side == "yes" else snapshot.no_price
            price_cents = _price_to_cents(raw_price)
            if price_cents is None:
                stats["paper_orders_skipped"] += 1
                continue
            price_cents = max(
                self.settings.paper_trade_min_price_cents,
                min(self.settings.paper_trade_max_price_cents, price_cents),
            )

            request_payload: dict[str, Any] = {
                "ticker": signal.market_ticker,
                "side": side,
                "count": self.settings.paper_trade_contract_count,
                "price_cents": price_cents,
            }
            response_payload: dict[str, Any] = {}
            status = "simulated"
            reason: str | None = None
            external_order_id: str | None = None

            stats["paper_orders_attempted"] += 1
            if self.settings.paper_trading_mode == "kalshi_demo":
                try:
                    response_payload = self.client.place_order(
                        ticker=signal.market_ticker,
                        side=side,
                        count=self.settings.paper_trade_contract_count,
                        price_cents=price_cents,
                        base_url=self.settings.paper_trading_base_url,
                    )
                    external_order_id = _extract_order_id(response_payload)
                    status = "submitted"
                    stats["paper_orders_submitted"] += 1
                except Exception as exc:
                    status = "failed"
                    reason = str(exc)
                    stats["paper_orders_failed"] += 1
                    logger.exception(
                        "paper_trade_submit_failed ticker=%s side=%s",
                        signal.market_ticker,
                        side,
                    )
            else:
                reason = "simulation_only"
                stats["paper_orders_simulated"] += 1

            orders.append(
                PaperTradeOrder(
                    market_ticker=signal.market_ticker,
                    signal_type=signal.signal_type,
                    direction=signal.direction,
                    side=side,
                    count=self.settings.paper_trade_contract_count,
                    limit_price_cents=price_cents,
                    provider=self.settings.paper_trading_mode,
                    status=status,
                    reason=reason,
                    external_order_id=external_order_id,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    created_at=now_utc,
                )
            )

        if orders:
            stats["paper_orders_recorded"] = self.store.insert_paper_trade_orders(orders)
        return orders, stats
