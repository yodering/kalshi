from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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

