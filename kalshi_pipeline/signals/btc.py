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


def _price_at_or_before(ticks: list[CryptoSpotTick], target_ts: datetime) -> float | None:
    eligible = [tick for tick in ticks if tick.ts <= target_ts]
    if not eligible:
        return None
    latest_ts = max(tick.ts for tick in eligible)
    return _median_price([tick for tick in eligible if tick.ts == latest_ts])


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
    latest_price = _median_price(current_ticks)
    if latest_price is None and recent_ticks:
        latest_ts = max(tick.ts for tick in recent_ticks)
        latest_price = _median_price([tick for tick in recent_ticks if tick.ts == latest_ts])
    if latest_price is None:
        return []

    lookback_target = now_utc - timedelta(minutes=settings.btc_momentum_lookback_minutes)
    anchor_price = _price_at_or_before(recent_ticks, lookback_target)
    if anchor_price is None:
        anchor_price = latest_price

    momentum_bps = ((latest_price / anchor_price) - 1.0) * 10000 if anchor_price else 0.0
    fair_shift = max(-0.35, min(0.35, momentum_bps / 800))
    fair_yes_prob = max(0.01, min(0.99, 0.5 + fair_shift))

    prices_this_tick = [tick.price_usd for tick in current_ticks if tick.price_usd > 0]
    cross_source_spread_bps = 0.0
    if len(prices_this_tick) >= 2 and latest_price > 0:
        cross_source_spread_bps = ((max(prices_this_tick) - min(prices_this_tick)) / latest_price) * 10000

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
                    "momentum_bps": round(momentum_bps, 2),
                    "cross_source_spread_bps": round(cross_source_spread_bps, 2),
                    "lookback_minutes": settings.btc_momentum_lookback_minutes,
                },
                created_at=now_utc,
            )
        )
    return signals

