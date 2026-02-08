from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class Market:
    ticker: str
    title: str
    status: str
    close_time: datetime | None
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class MarketSnapshot:
    ticker: str
    ts: datetime
    yes_price: float | None
    no_price: float | None
    volume: float | None
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class WeatherEnsembleSample:
    collected_at: datetime
    target_date: date
    model: str
    member: str
    max_temp_f: float
    source: str
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class CryptoSpotTick:
    ts: datetime
    source: str
    symbol: str
    price_usd: float
    raw_json: dict[str, Any]


@dataclass(frozen=True)
class SignalRecord:
    signal_type: str
    market_ticker: str | None
    direction: str
    model_probability: float | None
    market_probability: float | None
    edge_bps: float | None
    confidence: float | None
    details: dict[str, Any]
    created_at: datetime
