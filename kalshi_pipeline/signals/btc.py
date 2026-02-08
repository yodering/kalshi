from __future__ import annotations

from datetime import datetime, timedelta, timezone
import statistics

from ..config import Settings
from ..models import CryptoSpotTick, Market, MarketSnapshot, SignalRecord


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


def _median_price(ticks: list[CryptoSpotTick]) -> float | None:
    prices = [tick.price_usd for tick in ticks if tick.price_usd > 0]
    if not prices:
        return None
    return float(statistics.median(prices))


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


def _median_from_sources(
    source_prices: dict[str, float], ordered_sources: list[str]
) -> tuple[float | None, list[str]]:
    used_sources = [source for source in ordered_sources if source in source_prices]
    if not used_sources:
        return None, []
    values = [source_prices[source] for source in used_sources]
    return float(statistics.median(values)), used_sources


def _anchor_core_price(
    ticks: list[CryptoSpotTick],
    target_ts: datetime,
    core_sources: list[str],
    min_core_sources: int,
) -> tuple[float | None, datetime | None, list[str]]:
    candidate_timestamps = sorted(
        {tick.ts for tick in ticks if tick.ts <= target_ts},
        reverse=True,
    )
    for timestamp in candidate_timestamps:
        source_prices = _source_prices_at_timestamp(ticks, timestamp)
        price, used_sources = _median_from_sources(source_prices, core_sources)
        if price is None:
            continue
        if len(used_sources) < min_core_sources:
            continue
        return price, timestamp, used_sources
    return None, None, []


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

    # Prefer fresh ticks from this run; fall back to latest from DB history.
    active_ticks = current_ticks if current_ticks else recent_ticks
    latest_tick_ts, latest_source_prices = _latest_source_prices(active_ticks)
    if latest_tick_ts is None or not latest_source_prices:
        return []

    latest_price, latest_core_sources = _median_from_sources(
        latest_source_prices, settings.btc_core_sources
    )
    if latest_price is None:
        return []
    if len(latest_core_sources) < settings.btc_min_core_sources:
        return []

    lookback_target = now_utc - timedelta(minutes=settings.btc_momentum_lookback_minutes)
    anchor_price, anchor_ts, anchor_core_sources = _anchor_core_price(
        recent_ticks,
        lookback_target,
        settings.btc_core_sources,
        settings.btc_min_core_sources,
    )
    if anchor_price is None:
        anchor_price = latest_price
        anchor_ts = latest_tick_ts
        anchor_core_sources = latest_core_sources

    momentum_bps = ((latest_price / anchor_price) - 1.0) * 10000 if anchor_price else 0.0
    fair_shift = max(-0.35, min(0.35, momentum_bps / 800))
    fair_yes_prob = max(0.01, min(0.99, 0.5 + fair_shift))

    prices_this_tick = [latest_source_prices[source] for source in latest_core_sources]
    cross_source_spread_bps = 0.0
    if len(prices_this_tick) >= 2 and latest_price > 0:
        cross_source_spread_bps = ((max(prices_this_tick) - min(prices_this_tick)) / latest_price) * 10000

    binance_basis_bps = None
    binance_price = latest_source_prices.get("binance")
    if binance_price is not None and latest_price > 0:
        binance_basis_bps = ((binance_price - latest_price) / latest_price) * 10000

    all_sources_observed = sorted(latest_source_prices.keys())

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
        confidence = min(1.0, abs(edge_bps) / max(settings.signal_min_edge_bps * 3, 1))
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
                    "latest_spot": round(latest_price, 4),
                    "anchor_spot": round(anchor_price, 4),
                    "latest_tick_ts": latest_tick_ts.isoformat(),
                    "anchor_tick_ts": anchor_ts.isoformat() if anchor_ts else None,
                    "core_sources_used_latest": latest_core_sources,
                    "core_sources_used_anchor": anchor_core_sources,
                    "all_sources_observed": all_sources_observed,
                    "momentum_bps": round(momentum_bps, 2),
                    "cross_source_spread_bps": round(cross_source_spread_bps, 2),
                    "binance_basis_bps": round(binance_basis_bps, 2)
                    if binance_basis_bps is not None
                    else None,
                    "lookback_minutes": settings.btc_momentum_lookback_minutes,
                },
                created_at=now_utc,
            )
        )
    return signals
