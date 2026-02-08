from __future__ import annotations

from datetime import datetime
import re

from ..config import Settings
from ..models import (
    Market,
    MarketSnapshot,
    SignalRecord,
    WeatherBracketProbability,
    WeatherEnsembleSample,
)


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
        above_match = re.search(
            r"(?:above|at least|or above|and above)\s+(-?\d+(?:\.\d+)?)", normalized
        )
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


def build_weather_probabilities(
    markets: list[Market],
    snapshots_by_ticker: dict[str, MarketSnapshot],
    ensemble_samples: list[WeatherEnsembleSample],
    *,
    now_utc: datetime,
) -> list[WeatherBracketProbability]:
    if not ensemble_samples:
        return []
    relevant_markets = [market for market in markets if _is_weather_market(market)]
    if not relevant_markets:
        return []
    target_date = ensemble_samples[0].target_date
    rows: list[WeatherBracketProbability] = []
    for market in relevant_markets:
        bounds = _parse_bracket_bounds(market)
        if bounds is None:
            continue
        model_prob = _probability_for_bounds(ensemble_samples, bounds[0], bounds[1])
        if model_prob is None:
            continue
        snapshot = snapshots_by_ticker.get(market.ticker)
        market_prob = _normalize_probability(snapshot.yes_price if snapshot else None)
        edge = None if market_prob is None else (model_prob - market_prob)
        rows.append(
            WeatherBracketProbability(
                computed_at=now_utc,
                target_date=target_date,
                ticker=market.ticker,
                bracket_low=bounds[0],
                bracket_high=bounds[1],
                model_prob=round(model_prob, 6),
                market_prob=round(market_prob, 6) if market_prob is not None else None,
                edge=round(edge, 6) if edge is not None else None,
                ensemble_count=len(ensemble_samples),
            )
        )
    return rows


def build_weather_signals(
    settings: Settings,
    markets: list[Market],
    snapshots_by_ticker: dict[str, MarketSnapshot],
    ensemble_samples: list[WeatherEnsembleSample],
    *,
    now_utc: datetime,
) -> list[SignalRecord]:
    probability_rows = build_weather_probabilities(
        markets=markets,
        snapshots_by_ticker=snapshots_by_ticker,
        ensemble_samples=ensemble_samples,
        now_utc=now_utc,
    )
    if not probability_rows:
        return []

    sample_count = len(ensemble_samples)
    sample_strength = min(1.0, sample_count / 60.0)
    signals: list[SignalRecord] = []
    for row in probability_rows:
        if row.market_prob is None or row.edge is None:
            continue
        edge_bps = round(row.edge * 10000, 2)
        direction = _direction(edge_bps, settings.signal_min_edge_bps)
        if direction == "flat" and not settings.signal_store_all:
            continue
        edge_strength = min(
            1.0, abs(edge_bps) / max(float(settings.signal_min_edge_bps) * 3.0, 1.0)
        )
        confidence = round(max(0.0, min(1.0, sample_strength * edge_strength)), 4)
        signals.append(
            SignalRecord(
                signal_type="weather",
                market_ticker=row.ticker,
                direction=direction,
                model_probability=row.model_prob,
                market_probability=row.market_prob,
                edge_bps=edge_bps,
                confidence=confidence,
                details={
                    "lower_bound": row.bracket_low,
                    "upper_bound": row.bracket_high,
                    "sample_count": sample_count,
                    "target_date": str(row.target_date),
                },
                created_at=now_utc,
            )
        )
    return signals
