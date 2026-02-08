from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .manager import WSManager


@dataclass(frozen=True)
class CoinbaseTick:
    ts: datetime
    price: float
    best_bid: float | None
    best_ask: float | None


class CoinbaseFeed:
    def __init__(self, url: str = "wss://ws-feed.exchange.coinbase.com") -> None:
        self.url = url
        self.manager = WSManager(
            url=self.url,
            on_message=self._on_message,
            reconnect_delay=1.0,
            reconnect_max_delay=60.0,
        )
        self._ticks: deque[CoinbaseTick] = deque(maxlen=5000)

    async def _on_message(self, msg: dict[str, Any]) -> None:
        msg_type = str(msg.get("type", "")).lower()
        if msg_type == "subscriptions":
            return
        if msg_type != "ticker":
            return
        try:
            price = float(msg.get("price"))
        except Exception:
            return
        ts_raw = str(msg.get("time", ""))
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except Exception:
            ts = datetime.now(timezone.utc)
        try:
            best_bid = float(msg.get("best_bid")) if msg.get("best_bid") is not None else None
        except Exception:
            best_bid = None
        try:
            best_ask = float(msg.get("best_ask")) if msg.get("best_ask") is not None else None
        except Exception:
            best_ask = None
        self._ticks.append(
            CoinbaseTick(ts=ts, price=price, best_bid=best_bid, best_ask=best_ask)
        )

    async def run(self) -> None:
        self.manager._subscriptions.append(  # noqa: SLF001
            {
                "type": "subscribe",
                "product_ids": ["BTC-USD"],
                "channels": ["ticker"],
            }
        )
        await self.manager.run()

    def get_latest_price(self) -> float | None:
        if not self._ticks:
            return None
        return self._ticks[-1].price

    async def close(self) -> None:
        await self.manager.close()
