from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from .manager import WSManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeTick:
    ts: datetime
    price: float
    quantity: float


class BinanceFeed:
    def __init__(self, url: str = "wss://stream.binance.com:9443/ws/btcusdt@trade") -> None:
        self.url = url
        self.manager = WSManager(
            url=self.url,
            on_message=self._on_message,
            reconnect_delay=1.0,
            reconnect_max_delay=60.0,
        )
        self._ticks: deque[TradeTick] = deque(maxlen=5000)

    async def _on_message(self, msg: dict[str, Any]) -> None:
        if str(msg.get("e", "")).lower() != "trade":
            return
        try:
            price = float(msg.get("p"))
            quantity = float(msg.get("q"))
            ts_ms = int(msg.get("T"))
        except Exception:
            return
        ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        self._ticks.append(TradeTick(ts=ts, price=price, quantity=quantity))

    def get_latest_price(self) -> float | None:
        if not self._ticks:
            return None
        return self._ticks[-1].price

    def get_vwap(self, window_seconds: int) -> float | None:
        if not self._ticks:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, window_seconds))
        weighted = 0.0
        volume = 0.0
        for tick in reversed(self._ticks):
            if tick.ts < cutoff:
                break
            weighted += tick.price * tick.quantity
            volume += tick.quantity
        if volume <= 0:
            return None
        return weighted / volume

    def get_price_history(self, n: int) -> list[float]:
        if n <= 0:
            return []
        return [tick.price for tick in list(self._ticks)[-n:]]

    async def run(self) -> None:
        await self.manager.run()

    async def close(self) -> None:
        await self.manager.close()
