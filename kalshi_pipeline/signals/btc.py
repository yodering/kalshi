from __future__ import annotations

from datetime import datetime, timedelta

from ..config import Settings
from ..models import CryptoSpotTick, Market, MarketSnapshot, SignalRecord


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
) -> list[SignalRecord]:
    if not current_ticks and not recent_ticks:
        return []

    active_ticks = current_ticks if current_ticks else recent_ticks
    latest_ts, latest_source_prices = _latest_source_prices(active_ticks)
    if latest_ts is None or not latest_source_prices:
        return []

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
    for market in markets:
        if not _is_btc_market(market):
            continue
        snapshot = snapshots_by_ticker.get(market.ticker)
        market_prob = _normalize_probability(snapshot.yes_price if snapshot else None)
        if market_prob is None:
            continue
        edge_bps = round((fair_yes_prob - market_prob) * 10000, 2)
        direction = _direction(edge_bps, settings.signal_min_edge_bps)
        if direction == "flat" and not settings.signal_store_all:
            continue
        confidence = max(0.0, min(1.0, (latest_confidence + anchor_confidence) / 2.0))
        signals.append(
            SignalRecord(
                signal_type="btc",
                market_ticker=market.ticker,
                direction=direction,
                model_probability=round(fair_yes_prob, 6),
                market_probability=round(market_prob, 6),
                edge_bps=edge_bps,
                confidence=round(confidence, 4),
                details={
                    "latest_fair_value": round(latest_fair_value, 4),
                    "anchor_fair_value": round(anchor_fair_value, 4),
                    "latest_tick_ts": latest_ts.isoformat(),
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
                },
                created_at=now_utc,
            )
        )
    return signals
