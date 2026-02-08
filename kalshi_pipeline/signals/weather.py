from __future__ import annotations

from datetime import datetime
import re

from ..config import Settings
from ..models import Market, MarketSnapshot, SignalRecord, WeatherEnsembleSample


def _normalize_probability(price: float | None) -> float | None:
    if price is None:
        return None
    if price > 1.0:
        return max(0.0, min(1.0, price / 100.0))
    return max(0.0, min(1.0, price))


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_weather_market(market: Market) -> bool:
    if market.ticker.upper().startswith("KXHIGHNY"):
        return True
    series_ticker = str(market.raw_json.get("series_ticker", "")).upper()
    return series_ticker == "KXHIGHNY"


def _parse_bracket_bounds(market: Market) -> tuple[float | None, float | None] | None:
    raw = market.raw_json
    floor = _as_float(raw.get("floor_strike") or raw.get("floor"))
    cap = _as_float(raw.get("cap_strike") or raw.get("cap"))
    if floor is not None or cap is not None:
        return floor, cap

    candidates = [
        str(raw.get("subtitle", "")),
        str(raw.get("yes_sub_title", "")),
        str(raw.get("title", "")),
        market.title,
    ]
    for text in candidates:
        normalized = text.lower()
        below_match = re.search(r"below\s+(-?\d+(?:\.\d+)?)", normalized)
        if below_match:
            return None, float(below_match.group(1))
        above_match = re.search(r"(?:above|at least|or above|and above)\s+(-?\d+(?:\.\d+)?)", normalized)
        if above_match:
            return float(above_match.group(1)), None
        plus_match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:\+|or\s+higher)", normalized)
        if plus_match:
            return float(plus_match.group(1)), None
        range_match = re.search(
            r"(-?\d+(?:\.\d+)?)\s*(?:to|through|-|–)\s*(-?\d+(?:\.\d+)?)",
            normalized,
        )
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            if low.is_integer() and high.is_integer():
                return low, high + 1.0
            return low, high
    return None


def _probability_for_bounds(
    samples: list[WeatherEnsembleSample], lower: float | None, upper: float | None
) -> float | None:
    values = [sample.max_temp_f for sample in samples]
    if not values:
        return None
    hits = 0
    for value in values:
        if lower is not None and value < lower:
            continue
        if upper is not None and value >= upper:
            continue
        hits += 1
    return hits / len(values)


def _direction(edge_bps: float | None, min_edge_bps: int) -> str:
    if edge_bps is None:
        return "flat"
    if edge_bps >= min_edge_bps:
        return "buy_yes"
    if edge_bps <= -min_edge_bps:
        return "buy_no"
    return "flat"


def build_weather_signals(
    settings: Settings,
    markets: list[Market],
    snapshots_by_ticker: dict[str, MarketSnapshot],
    ensemble_samples: list[WeatherEnsembleSample],
    *,
    now_utc: datetime,
) -> list[SignalRecord]:
    if not ensemble_samples:
        return []
    relevant_markets = [market for market in markets if _is_weather_market(market)]
    signals: list[SignalRecord] = []
    for market in relevant_markets:
        bounds = _parse_bracket_bounds(market)
        if bounds is None:
            continue
        model_prob = _probability_for_bounds(ensemble_samples, bounds[0], bounds[1])
        market_snapshot = snapshots_by_ticker.get(market.ticker)
        market_prob = _normalize_probability(
            market_snapshot.yes_price if market_snapshot is not None else None
        )
        if model_prob is None or market_prob is None:
            continue
        edge_bps = round((model_prob - market_prob) * 10000, 2)
        direction = _direction(edge_bps, settings.signal_min_edge_bps)
        if direction == "flat" and not settings.signal_store_all:
            continue
        confidence = min(1.0, abs(edge_bps) / max(settings.signal_min_edge_bps * 3, 1))
        signals.append(
            SignalRecord(
                signal_type="weather",
                market_ticker=market.ticker,
                direction=direction,
                model_probability=round(model_prob, 6),
                market_probability=round(market_prob, 6),
                edge_bps=edge_bps,
                confidence=round(confidence, 4),
                details={
                    "lower_bound": bounds[0],
                    "upper_bound": bounds[1],
                    "sample_count": len(ensemble_samples),
                    "target_date": str(ensemble_samples[0].target_date),
                },
                created_at=now_utc,
            )
        )
    return signals

