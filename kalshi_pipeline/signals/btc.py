from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..config import Settings
from ..models import CryptoSpotTick, Market, MarketSnapshot, SignalRecord
from ..orderbook_utils import effective_no_ask_vwap, effective_yes_ask_vwap

if TYPE_CHECKING:
    from ..data.price_provider import PriceProvider


SOURCE_WEIGHTS: dict[str, float] = {
    "coinbase": 0.30,
    "kraken": 0.20,
    "bitstamp": 0.15,
    "binance": 0.25,
}


def _normalize_probability(price: float | None) -> float | None:
    if price is None:
        return None
    if price > 1.0:
        return max(0.0, min(1.0, price / 100.0))
    return max(0.0, min(1.0, price))


def _is_btc_market(market: Market) -> bool:
    if market.ticker.upper().startswith("KXBTC15M"):
        return True
    series_ticker = str(market.raw_json.get("series_ticker", "")).upper()
    return series_ticker == "KXBTC15M"


def _source_prices_at_timestamp(
    ticks: list[CryptoSpotTick], target_ts: datetime
) -> dict[str, float]:
    prices: dict[str, float] = {}
    for tick in ticks:
        if tick.ts != target_ts:
            continue
        if tick.price_usd <= 0:
            continue
        prices[tick.source] = tick.price_usd
    return prices


def _latest_source_prices(
    ticks: list[CryptoSpotTick],
) -> tuple[datetime | None, dict[str, float]]:
    if not ticks:
        return None, {}
    latest_ts = max(tick.ts for tick in ticks)
    return latest_ts, _source_prices_at_timestamp(ticks, latest_ts)


def _weighted_fair_value(
    source_prices: dict[str, float],
) -> tuple[float | None, float, list[str], float]:
    weighted_sum = 0.0
    total_weight = 0.0
    used_sources: list[str] = []
    for source, price in source_prices.items():
        if price <= 0:
            continue
        weight = SOURCE_WEIGHTS.get(source, 0.0)
        if weight <= 0:
            continue
        weighted_sum += price * weight
        total_weight += weight
        used_sources.append(source)
    if total_weight <= 0:
        return None, 0.0, [], 0.0
    fair_value = weighted_sum / total_weight
    agreement = 1.0
    if len(used_sources) >= 2 and fair_value > 0:
        spread = max(source_prices[s] for s in used_sources) - min(
            source_prices[s] for s in used_sources
        )
        spread_bps = (spread / fair_value) * 10000
        agreement = max(0.0, 1.0 - min(1.0, spread_bps / 100.0))
    elif len(used_sources) == 1:
        agreement = 0.7
    confidence = max(0.0, min(1.0, total_weight * agreement))
    return fair_value, confidence, sorted(used_sources), agreement


def _find_anchor_snapshot(
    ticks: list[CryptoSpotTick], lookback_target: datetime
) -> tuple[float | None, datetime | None, dict[str, float], float]:
    candidate_timestamps = sorted(
        {tick.ts for tick in ticks if tick.ts <= lookback_target},
        reverse=True,
    )
    for timestamp in candidate_timestamps:
        source_prices = _source_prices_at_timestamp(ticks, timestamp)
        fair_value, confidence, _used, _agreement = _weighted_fair_value(source_prices)
        if fair_value is None:
            continue
        return fair_value, timestamp, source_prices, confidence
    return None, None, {}, 0.0


def _direction(edge_bps: float | None, min_edge_bps: int) -> str:
    if edge_bps is None:
        return "flat"
    if edge_bps >= min_edge_bps:
        return "buy_yes"
    if edge_bps <= -min_edge_bps:
        return "buy_no"
    return "flat"


def build_btc_signals(
    settings: Settings,
    markets: list[Market],
    snapshots_by_ticker: dict[str, MarketSnapshot],
    recent_ticks: list[CryptoSpotTick],
    current_ticks: list[CryptoSpotTick],
    *,
    now_utc: datetime,
    price_provider: "PriceProvider | None" = None,
    orderbooks_by_ticker: dict[str, dict[str, object]] | None = None,
) -> list[SignalRecord]:
    latest_source_prices: dict[str, float] = {}
    latest_ts: datetime | None = None
    ws_price_sources: dict[str, str] = {}

    if price_provider is not None:
        live_prices = price_provider.get_btc_prices()
        for source_name, snapshot in live_prices.items():
            latest_source_prices[source_name] = float(snapshot.price)
            ws_price_sources[source_name] = snapshot.source
            if latest_ts is None or snapshot.timestamp > latest_ts:
                latest_ts = snapshot.timestamp

    if not latest_source_prices:
        if not current_ticks and not recent_ticks:
            return []
        active_ticks = current_ticks if current_ticks else recent_ticks
        latest_ts, latest_source_prices = _latest_source_prices(active_ticks)
        if latest_ts is None or not latest_source_prices:
            return []
        ws_price_sources = {source: "rest" for source in latest_source_prices}

    latest_fair_value, latest_confidence, latest_used_sources, agreement = _weighted_fair_value(
        latest_source_prices
    )
    if latest_fair_value is None:
        return []

    lookback_target = now_utc - timedelta(minutes=settings.btc_momentum_lookback_minutes)
    anchor_fair_value, anchor_ts, anchor_source_prices, anchor_confidence = _find_anchor_snapshot(
        recent_ticks, lookback_target
    )
    if anchor_fair_value is None:
        anchor_fair_value = latest_fair_value
        anchor_ts = latest_ts
        anchor_source_prices = latest_source_prices
        anchor_confidence = latest_confidence

    momentum_bps = (
        ((latest_fair_value / anchor_fair_value) - 1.0) * 10000
        if anchor_fair_value
        else 0.0
    )
    fair_shift = max(-0.35, min(0.35, momentum_bps / 800))
    fair_yes_prob = max(0.01, min(0.99, 0.5 + fair_shift))
    missing_sources = sorted(
        source for source in settings.btc_enabled_sources if source not in latest_source_prices
    )

    signals: list[SignalRecord] = []
    target_qty = max(1, settings.paper_trade_contract_count)
    for market in markets:
        if not _is_btc_market(market):
            continue
        snapshot = snapshots_by_ticker.get(market.ticker)
        if snapshot is None and price_provider is not None:
            snapshot = price_provider.get_market_snapshot(market.ticker)
            if snapshot is not None:
                snapshots_by_ticker[market.ticker] = snapshot

        orderbook = None
        if price_provider is not None:
            orderbook = price_provider.get_kalshi_orderbook(market.ticker)
        if orderbook is None and orderbooks_by_ticker is not None:
            maybe_book = orderbooks_by_ticker.get(market.ticker)
            if isinstance(maybe_book, dict):
                orderbook = maybe_book
        if orderbook is None and snapshot is not None and isinstance(snapshot.raw_json, dict):
            maybe_book = snapshot.raw_json.get("orderbook")
            if isinstance(maybe_book, dict):
                orderbook = maybe_book

        market_prob_default = _normalize_probability(snapshot.yes_price if snapshot else None)
        yes_vwap = (
            effective_yes_ask_vwap(orderbook, target_qty)
            if isinstance(orderbook, dict)
            else None
        )
        no_vwap = (
            effective_no_ask_vwap(orderbook, target_qty)
            if isinstance(orderbook, dict)
            else None
        )

        yes_market_prob = (
            _normalize_probability(yes_vwap[0] / 100.0) if yes_vwap is not None else market_prob_default
        )
        no_implied_yes_prob = (
            _normalize_probability(1.0 - (no_vwap[0] / 100.0))
            if no_vwap is not None
            else market_prob_default
        )

        yes_edge = None
        if yes_market_prob is not None:
            yes_edge = (fair_yes_prob - yes_market_prob) * 10000

        no_edge = None
        if no_implied_yes_prob is not None:
            no_edge = (fair_yes_prob - no_implied_yes_prob) * 10000

        edge_candidates = [edge for edge in (yes_edge, no_edge) if edge is not None]
        if not edge_candidates:
            continue

        # Choose the actionable side with the strongest absolute edge after liquidity adjustment.
        selected_edge = max(edge_candidates, key=lambda item: abs(item))
        direction = _direction(selected_edge, settings.signal_min_edge_bps)
        if direction == "flat" and not settings.signal_store_all:
            continue

        if direction == "buy_yes":
            market_prob = yes_market_prob
            selected_vwap = yes_vwap
            selected_fillable = yes_vwap[1] if yes_vwap is not None else None
        elif direction == "buy_no":
            market_prob = no_implied_yes_prob
            selected_vwap = no_vwap
            selected_fillable = no_vwap[1] if no_vwap is not None else None
        else:
            market_prob = yes_market_prob if yes_market_prob is not None else no_implied_yes_prob
            selected_vwap = yes_vwap if yes_vwap is not None else no_vwap
            selected_fillable = selected_vwap[1] if selected_vwap is not None else None

        if market_prob is None:
            continue

        edge_bps = round((fair_yes_prob - market_prob) * 10000, 2)
        confidence = max(0.0, min(1.0, (latest_confidence + anchor_confidence) / 2.0))

        price_source_values = [
            ws_price_sources.get(source, "rest")
            for source in latest_used_sources
            if source in ws_price_sources
        ]
        orderbook_source = (
            str(orderbook.get("source", "rest")) if isinstance(orderbook, dict) else "rest"
        )
        if price_source_values and all(source == "ws" for source in price_source_values) and orderbook_source == "ws":
            data_source = "ws"
        elif "ws" in price_source_values or orderbook_source == "ws":
            data_source = "mixed"
        elif "rest_fallback" in price_source_values:
            data_source = "rest_fallback"
        else:
            data_source = "rest"

        signals.append(
            SignalRecord(
                signal_type="btc",
                market_ticker=market.ticker,
                direction=direction,
                model_probability=round(fair_yes_prob, 6),
                market_probability=round(market_prob, 6),
                edge_bps=edge_bps,
                confidence=round(confidence, 4),
                data_source=data_source,
                vwap_cents=(round(selected_vwap[0], 4) if selected_vwap is not None else None),
                fillable_qty=selected_fillable,
                liquidity_sufficient=(
                    bool(selected_fillable is not None and selected_fillable >= target_qty)
                    if selected_fillable is not None
                    else None
                ),
                details={
                    "latest_fair_value": round(latest_fair_value, 4),
                    "anchor_fair_value": round(anchor_fair_value, 4),
                    "latest_tick_ts": latest_ts.isoformat(),
                    "signal_latency_ms": (
                        round(max(0.0, (now_utc - latest_ts).total_seconds() * 1000.0), 2)
                        if latest_ts is not None
                        else None
                    ),
                    "anchor_tick_ts": anchor_ts.isoformat() if anchor_ts else None,
                    "momentum_bps": round(momentum_bps, 2),
                    "source_prices_latest": {
                        k: round(v, 4) for k, v in latest_source_prices.items()
                    },
                    "source_prices_anchor": {
                        k: round(v, 4) for k, v in anchor_source_prices.items()
                    },
                    "sources_used_latest": latest_used_sources,
                    "missing_sources_latest": missing_sources,
                    "source_weight_coverage": round(
                        sum(SOURCE_WEIGHTS.get(source, 0.0) for source in latest_used_sources), 4
                    ),
                    "agreement_factor": round(agreement, 4),
                    "target_qty": target_qty,
                    "yes_vwap": round(yes_vwap[0], 4) if yes_vwap is not None else None,
                    "yes_fillable": yes_vwap[1] if yes_vwap is not None else None,
                    "no_vwap": round(no_vwap[0], 4) if no_vwap is not None else None,
                    "no_fillable": no_vwap[1] if no_vwap is not None else None,
                    "orderbook_source": orderbook_source,
                },
                created_at=now_utc,
            )
        )
    return signals
