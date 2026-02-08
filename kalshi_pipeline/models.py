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


@dataclass(frozen=True)
class PaperTradeOrder:
    market_ticker: str
    signal_type: str
    direction: str
    side: str
    count: int
    limit_price_cents: int
    provider: str
    status: str
    reason: str | None
    external_order_id: str | None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class AlertEvent:
    channel: str
    event_type: str
    market_ticker: str | None
    message: str
    status: str
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class WeatherBracketProbability:
    computed_at: datetime
    target_date: date
    ticker: str
    bracket_low: float | None
    bracket_high: float | None
    model_prob: float
    market_prob: float | None
    edge: float | None
    ensemble_count: int


@dataclass(frozen=True)
class MarketResolution:
    ticker: str
    series_ticker: str | None
    event_ticker: str | None
    market_type: str
    resolved_at: datetime | None
    result: str | None
    actual_value: float | None
    resolution_source: str
    collected_at: datetime


@dataclass(frozen=True)
class PredictionAccuracy:
    signal_id: int | None
    ticker: str
    signal_time: datetime
    model_prob: float | None
    market_prob: float | None
    edge_bps: float | None
    actual_outcome: bool | None
    pnl_per_contract: float | None
    created_at: datetime
