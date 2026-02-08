from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient
from .models import MarketSnapshot, PaperTradeOrder, SignalRecord
from .order_utils import as_int, extract_order_status, extract_queue_positions
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


def _ticker_prefix(ticker: str) -> str:
    cleaned = ticker.strip().upper()
    if not cleaned:
        return ""
    return cleaned.split("-", 1)[0]


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
        fill_probability: float | None = None,
    ) -> PaperTradeOrder:
        request_payload: dict[str, Any] = {
            "ticker": market_ticker,
            "side": side,
            "count": count,
            "price_cents": price_cents,
        }
        if fill_probability is not None:
            request_payload["fill_probability_estimate"] = round(float(fill_probability), 6)
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
        *,
        arb_opportunities: list[dict[str, Any]] | None = None,
    ) -> tuple[list[PaperTradeOrder], dict[str, int], list[dict[str, Any]]]:
        stats = {
            "paper_orders_candidates": 0,
            "paper_orders_attempted": 0,
            "paper_orders_submitted": 0,
            "paper_orders_simulated": 0,
            "paper_orders_failed": 0,
            "paper_orders_skipped": 0,
            "paper_orders_recorded": 0,
        }
        arb_results: list[dict[str, Any]] = []
        if not self.settings.paper_trading_enabled:
            return [], stats, arb_results

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
        fill_probability_cache: dict[str, float] = {}
        cooldown_since = now_utc - timedelta(minutes=self.settings.paper_trade_cooldown_minutes)
        max_orders = self.settings.paper_trade_max_orders_per_cycle

        # Arbitrage gets first priority if enabled.
        if self.settings.paper_trade_enable_arbitrage and arb_opportunities:
            for opportunity in arb_opportunities:
                legs = opportunity.get("legs")
                if not isinstance(legs, list) or not legs:
                    continue
                max_sets = as_int(opportunity.get("max_sets")) or 0
                count = min(max_sets, self.settings.paper_trade_contract_count)
                if count <= 0:
                    continue
                # Arbitrage legs should be treated atomically. If this is the first thing
                # in the cycle, allow it even if it exceeds the generic per-cycle cap.
                if stats["paper_orders_attempted"] > 0 and (
                    stats["paper_orders_attempted"] + len(legs) > max_orders
                ):
                    break

                result = {
                    "id": opportunity.get("id"),
                    "event_ticker": opportunity.get("event_ticker"),
                    "arb_type": opportunity.get("arb_type"),
                    "submitted": 0,
                    "simulated": 0,
                    "failed": 0,
                }
                for leg in legs:
                    if not isinstance(leg, dict):
                        continue
                    ticker = str(leg.get("ticker") or "").strip()
                    side = str(leg.get("side") or "").strip().lower()
                    price_cents = as_int(leg.get("price_cents"))
                    if not ticker or side not in {"yes", "no"} or price_cents is None:
                        continue
                    stats["paper_orders_attempted"] += 1
                    order = self._submit_order(
                        market_ticker=ticker,
                        signal_type="arbitrage",
                        direction=f"arb_{opportunity.get('arb_type') or 'combo'}",
                        side=side,
                        count=count,
                        price_cents=price_cents,
                        now_utc=now_utc,
                        fill_probability=None,
                    )
                    orders.append(order)
                    if order.status == "submitted":
                        stats["paper_orders_submitted"] += 1
                        result["submitted"] += 1
                        current_exposure_dollars += (
                            order.count * (order.limit_price_cents / 100.0)
                        )
                    elif order.status == "simulated":
                        stats["paper_orders_simulated"] += 1
                        result["simulated"] += 1
                    else:
                        stats["paper_orders_failed"] += 1
                        result["failed"] += 1
                result["executed"] = bool(result["submitted"] or result["simulated"])
                arb_results.append(result)

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
            fill_probability = self._estimate_fill_probability_for_signal(
                signal=signal,
                market_ticker=ticker,
                market_price_cents=price_cents,
                cache=fill_probability_cache,
            )
            count = compute_order_size(
                signal=signal,
                side=side,
                market_price_cents=price_cents,
                settings=self.settings,
                current_exposure_dollars=current_exposure_dollars,
                bankroll_dollars=self.settings.paper_trade_max_portfolio_exposure_dollars,
                fill_probability=fill_probability,
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
                fill_probability=fill_probability,
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
        return orders, stats, arb_results

    def reconcile_open_orders(
        self,
        *,
        signals: list[SignalRecord],
        snapshots_by_ticker: dict[str, MarketSnapshot],
        now_utc: datetime,
        allow_reprice: bool,
    ) -> tuple[list[PaperTradeOrder], dict[str, int]]:
        stats = {
            "paper_order_events_inserted": 0,
            "paper_orders_status_updates": 0,
            "paper_orders_filled": 0,
            "paper_orders_canceled": 0,
            "paper_orders_failed_reconcile": 0,
            "paper_orders_repriced": 0,
            "paper_orders_reprice_recorded": 0,
            "paper_orders_reprice_failed": 0,
            "paper_orders_queue_alerted": 0,
        }
        if self.settings.paper_trading_mode != "kalshi_demo":
            return [], stats
        if not self.settings.paper_trade_enable_queue_management:
            return [], stats

        active_orders = self.store.get_submitted_paper_orders(
            limit=250,
            since_ts=now_utc - timedelta(hours=24),
        )
        if not active_orders:
            return [], stats

        order_ids = [int(order["id"]) for order in active_orders]
        latest_events = self.store.get_latest_order_events(order_ids)
        market_tickers = sorted(
            {str(order["market_ticker"]) for order in active_orders if order.get("market_ticker")}
        )

        signal_by_ticker: dict[str, SignalRecord] = {}
        for signal in sorted(signals, key=lambda row: abs(row.edge_bps or 0.0), reverse=True):
            ticker = signal.market_ticker
            if ticker is None:
                continue
            if ticker in signal_by_ticker:
                continue
            signal_by_ticker[ticker] = signal

        repriced_orders: list[PaperTradeOrder] = []
        still_submitted: list[dict[str, object]] = []
        for order in active_orders:
            order_id = int(order["id"])
            external_order_id = str(order.get("external_order_id") or "").strip()
            if not external_order_id:
                continue
            ticker = str(order.get("market_ticker") or "").strip()
            if not ticker:
                continue

            try:
                payload = self.client.get_order(
                    external_order_id,
                    base_url=self.settings.paper_trading_base_url,
                )
            except Exception as exc:
                logger.warning(
                    "paper_trade_order_status_check_failed order_id=%s external_order_id=%s",
                    order_id,
                    external_order_id,
                    exc_info=True,
                )
                self.store.insert_order_event(
                    order_id=order_id,
                    market_ticker=ticker,
                    external_order_id=external_order_id,
                    status="status_check_failed",
                    event_ts=now_utc,
                    details={"reason": str(exc)},
                )
                stats["paper_order_events_inserted"] += 1
                continue

            normalized_status = extract_order_status(payload)
            last_event = latest_events.get(order_id, {})
            last_status = str(last_event.get("status") or "")
            is_open_status = normalized_status in {"submitted", "partially_filled"}
            if not is_open_status:
                updated = self.store.update_paper_trade_order_status(
                    order_id=order_id,
                    status=normalized_status,
                    reason=None,
                    response_payload=payload if isinstance(payload, dict) else {},
                )
                if updated:
                    stats["paper_orders_status_updates"] += 1
                if normalized_status == "filled":
                    stats["paper_orders_filled"] += 1
                elif normalized_status == "canceled":
                    stats["paper_orders_canceled"] += 1
                elif normalized_status == "failed":
                    stats["paper_orders_failed_reconcile"] += 1
                if last_status != normalized_status:
                    self.store.insert_order_event(
                        order_id=order_id,
                        market_ticker=ticker,
                        external_order_id=external_order_id,
                        status=normalized_status,
                        event_ts=now_utc,
                        details={"status_payload": payload if isinstance(payload, dict) else {}},
                    )
                    stats["paper_order_events_inserted"] += 1
                continue

            if normalized_status == "partially_filled":
                updated = self.store.update_paper_trade_order_status(
                    order_id=order_id,
                    status=normalized_status,
                    reason=None,
                    response_payload=payload if isinstance(payload, dict) else {},
                )
                if updated:
                    stats["paper_orders_status_updates"] += 1

            if last_status != normalized_status:
                self.store.insert_order_event(
                    order_id=order_id,
                    market_ticker=ticker,
                    external_order_id=external_order_id,
                    status=normalized_status,
                    event_ts=now_utc,
                    details={"status_payload": payload if isinstance(payload, dict) else {}},
                )
                stats["paper_order_events_inserted"] += 1

            still_submitted.append(order)

        if not still_submitted:
            return repriced_orders, stats

        queue_positions: dict[str, int] = {}
        if market_tickers:
            try:
                queue_payload = self.client.get_queue_positions(
                    market_tickers,
                    base_url=self.settings.paper_trading_base_url,
                )
                if isinstance(queue_payload, dict):
                    queue_positions = extract_queue_positions(queue_payload)
            except Exception:
                logger.warning("paper_trade_queue_positions_failed", exc_info=True)

        stale_cutoff = now_utc - timedelta(minutes=self.settings.paper_trade_queue_stale_minutes)
        for order in still_submitted:
            order_id = int(order["id"])
            ticker = str(order.get("market_ticker") or "").strip()
            external_order_id = str(order.get("external_order_id") or "").strip()
            side = str(order.get("side") or "").lower()
            order_created_at = order.get("created_at")
            if not ticker or not external_order_id:
                continue
            queue_position = queue_positions.get(external_order_id)
            if queue_position is None:
                queue_position = queue_positions.get(ticker)

            last_event = latest_events.get(order_id, {})
            last_status = str(last_event.get("status") or "")
            last_queue = as_int(last_event.get("queue_position"))
            if queue_position is not None and (last_status != "resting" or last_queue != queue_position):
                self.store.insert_order_event(
                    order_id=order_id,
                    market_ticker=ticker,
                    external_order_id=external_order_id,
                    status="resting",
                    queue_position=queue_position,
                    event_ts=now_utc,
                    details={},
                )
                stats["paper_order_events_inserted"] += 1

            if queue_position is None:
                continue
            if queue_position <= self.settings.paper_trade_queue_max_depth:
                continue
            stats["paper_orders_queue_alerted"] += 1
            if not allow_reprice:
                continue
            if not isinstance(order_created_at, datetime) or order_created_at > stale_cutoff:
                continue
            recent_reprices = self.store.get_recent_reprice_timestamps(
                market_ticker=ticker,
                since_ts=now_utc
                - timedelta(seconds=self.settings.paper_trade_reprice_window_seconds),
                limit=max(10, self.settings.paper_trade_reprice_max_per_window * 3),
            )
            if len(recent_reprices) >= self.settings.paper_trade_reprice_max_per_window:
                logger.info(
                    "paper_trade_reprice_blocked ticker=%s reason=max_reprices_per_window",
                    ticker,
                )
                continue
            if recent_reprices:
                most_recent = max(recent_reprices)
                since_last = (now_utc - most_recent).total_seconds()
                if since_last < self.settings.paper_trade_reprice_cooldown_seconds:
                    logger.info(
                        "paper_trade_reprice_blocked ticker=%s reason=cooldown_seconds",
                        ticker,
                    )
                    continue
            current_signal = signal_by_ticker.get(ticker)
            expected_direction = "buy_yes" if side == "yes" else "buy_no"
            if current_signal is None or current_signal.direction != expected_direction:
                continue
            snapshot = snapshots_by_ticker.get(ticker)
            if snapshot is None:
                continue
            try:
                cancel_payload = self.client.cancel_order(
                    external_order_id,
                    base_url=self.settings.paper_trading_base_url,
                )
                self.store.update_paper_trade_order_status(
                    order_id=order_id,
                    status="canceled",
                    reason="queue_reprice_cancel",
                    response_payload=cancel_payload if isinstance(cancel_payload, dict) else {},
                )
                self.store.insert_order_event(
                    order_id=order_id,
                    market_ticker=ticker,
                    external_order_id=external_order_id,
                    status="canceled",
                    queue_position=queue_position,
                    event_ts=now_utc,
                    details={"reason": "queue_reprice_cancel"},
                )
                stats["paper_order_events_inserted"] += 1
                stats["paper_orders_canceled"] += 1
            except Exception as exc:
                logger.warning(
                    "paper_trade_reprice_cancel_failed order_id=%s external_order_id=%s",
                    order_id,
                    external_order_id,
                    exc_info=True,
                )
                self.store.insert_order_event(
                    order_id=order_id,
                    market_ticker=ticker,
                    external_order_id=external_order_id,
                    status="queue_refresh_failed",
                    queue_position=queue_position,
                    event_ts=now_utc,
                    details={"reason": str(exc)},
                )
                stats["paper_order_events_inserted"] += 1
                stats["paper_orders_reprice_failed"] += 1
                continue

            book = _best_book_prices(snapshot)
            new_price = _maker_price_for_side(
                side=side,
                book=book,
                maker_only=self.settings.paper_trade_maker_only,
                min_price_cents=self.settings.paper_trade_min_price_cents,
                max_price_cents=self.settings.paper_trade_max_price_cents,
            )
            if new_price is None:
                continue
            old_price = as_int(order.get("limit_price_cents"))
            if old_price is not None and new_price == old_price:
                continue
            refreshed = self._submit_order(
                market_ticker=ticker,
                signal_type=str(order.get("signal_type") or current_signal.signal_type),
                direction=expected_direction,
                side=side,
                count=int(order.get("count") or 1),
                price_cents=new_price,
                now_utc=now_utc,
                fill_probability=self._estimate_fill_probability_for_signal(
                    signal=current_signal,
                    market_ticker=ticker,
                    market_price_cents=new_price,
                    cache=None,
                ),
            )
            repriced_orders.append(refreshed)
            if refreshed.status == "submitted":
                stats["paper_orders_repriced"] += 1
                self.store.insert_order_event(
                    order_id=order_id,
                    market_ticker=ticker,
                    external_order_id=refreshed.external_order_id,
                    status="reprice_submitted",
                    queue_position=queue_position,
                    event_ts=now_utc,
                    details={
                        "old_order_id": external_order_id,
                        "old_price_cents": old_price,
                        "new_price_cents": new_price,
                    },
                )
                stats["paper_order_events_inserted"] += 1
            elif refreshed.status == "failed":
                stats["paper_orders_reprice_failed"] += 1

        if repriced_orders:
            stats["paper_orders_reprice_recorded"] = self.store.insert_paper_trade_orders(
                repriced_orders
            )
        return repriced_orders, stats

    def _estimate_fill_probability_for_signal(
        self,
        *,
        signal: SignalRecord,
        market_ticker: str,
        market_price_cents: int,
        cache: dict[str, float] | None,
    ) -> float:
        prefix = _ticker_prefix(market_ticker)
        if not prefix:
            return self.settings.paper_trade_default_fill_probability
        if cache is not None and prefix in cache:
            return cache[prefix]

        estimated = self.store.estimate_fill_probability(
            ticker_prefix=prefix,
            lookback_days=self.settings.paper_trade_fill_prob_lookback_days,
            min_price_cents=max(1, market_price_cents - 10),
            max_price_cents=min(99, market_price_cents + 10),
            min_samples=20,
        )
        if estimated is None:
            estimated = self.settings.paper_trade_default_fill_probability
        estimated = max(0.0, min(1.0, float(estimated)))
        if cache is not None:
            cache[prefix] = estimated
        logger.info(
            "paper_trade_fill_probability signal_type=%s ticker=%s estimated=%.4f",
            signal.signal_type,
            market_ticker,
            estimated,
        )
        return estimated
