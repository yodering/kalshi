from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .manager import WSManager


@dataclass(frozen=True)
class KrakenTick:
    ts: datetime
    price: float
    best_bid: float | None
    best_ask: float | None


class KrakenFeed:
    def __init__(self, url: str = "wss://ws.kraken.com/v2") -> None:
        self.url = url
        self.manager = WSManager(
            url=self.url,
            on_message=self._on_message,
            reconnect_delay=1.0,
            reconnect_max_delay=60.0,
        )
        self._ticks: deque[KrakenTick] = deque(maxlen=5000)
        self._last_update_time: datetime | None = None

    async def _on_message(self, msg: dict[str, Any]) -> None:
        channel = str(msg.get("channel", "")).lower()
        if channel != "ticker":
            return
        data = msg.get("data")
        if not isinstance(data, list) or not data:
            return
        row = data[0]
        if not isinstance(row, dict):
            return
        try:
            price = float(row.get("last"))
        except Exception:
            return
        try:
            best_bid = float(row.get("bid")) if row.get("bid") is not None else None
        except Exception:
            best_bid = None
        try:
            best_ask = float(row.get("ask")) if row.get("ask") is not None else None
        except Exception:
            best_ask = None
        self._ticks.append(
            KrakenTick(
                ts=datetime.now(timezone.utc),
                price=price,
                best_bid=best_bid,
                best_ask=best_ask,
            )
        )
        self._last_update_time = self._ticks[-1].ts

    @property
    def is_connected(self) -> bool:
        return self.manager.is_connected

    @property
    def last_update_time(self) -> datetime | None:
        return self._last_update_time

    @property
    def age_seconds(self) -> float:
        if self._last_update_time is None:
            return float("inf")
        return max(0.0, (datetime.now(timezone.utc) - self._last_update_time).total_seconds())

    async def run(self) -> None:
        self.manager._subscriptions.append(  # noqa: SLF001
            {
                "method": "subscribe",
                "params": {"channel": "ticker", "symbol": ["BTC/USD"]},
            }
        )
        await self.manager.run()

    def get_latest_price(self) -> float | None:
        if not self._ticks:
            return None
        return self._ticks[-1].price

    async def close(self) -> None:
        await self.manager.close()
