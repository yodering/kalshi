from __future__ import annotations

from collections import defaultdict
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..kalshi_client import KalshiClient
from .manager import WSManager

logger = logging.getLogger(__name__)


def build_kalshi_ws_url(base_url: str) -> str:
    parts = urlsplit(base_url.rstrip("/"))
    scheme = "wss"
    path = "/trade-api/ws/v2"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class KalshiFeed:
    def __init__(
        self,
        *,
        client: KalshiClient,
        ws_url: str,
    ) -> None:
        self.client = client
        self.ws_url = ws_url
        self.manager = WSManager(
            url=ws_url,
            auth_headers_provider=self._auth_headers,
            on_message=self._on_message,
            reconnect_delay=1.0,
            reconnect_max_delay=60.0,
        )
        self.orderbooks: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"yes": {}, "no": {}, "seq": None, "best_yes_bid": None, "best_yes_ask": None}
        )
        self._lifecycle_callbacks: list[callable] = []

    def _auth_headers(self) -> dict[str, str]:
        return self.client._build_auth_headers(  # noqa: SLF001
            method="GET", path="/trade-api/ws/v2"
        )

    async def subscribe_market(self, ticker: str) -> None:
        await self.manager.subscribe(
            channels=["orderbook_delta", "ticker"],
            tickers=[ticker],
        )

    async def subscribe_lifecycle(self) -> None:
        await self.manager.subscribe(channels=["market_lifecycle_v2"], tickers=[])

    def add_lifecycle_callback(self, callback: callable) -> None:
        self._lifecycle_callbacks.append(callback)

    def get_orderbook(self, ticker: str) -> dict[str, Any]:
        return self.orderbooks[ticker]

    def get_best_bid_ask(self, ticker: str) -> tuple[int | None, int | None]:
        state = self.orderbooks.get(ticker)
        if not state:
            return None, None
        return state.get("best_yes_bid"), state.get("best_yes_ask")

    async def _on_message(self, message: dict[str, Any]) -> None:
        msg_type = str(
            message.get("type")
            or message.get("msg_type")
            or message.get("channel")
            or ""
        ).lower()
        if "snapshot" in msg_type:
            self._handle_orderbook_snapshot(message)
            return
        if "orderbook_delta" in msg_type or "delta" in msg_type:
            self._handle_orderbook_delta(message)
            return
        if "ticker" in msg_type:
            self._handle_ticker(message)
            return
        if "lifecycle" in msg_type:
            self._handle_lifecycle(message)

    def _handle_orderbook_snapshot(self, msg: dict[str, Any]) -> None:
        ticker = str(msg.get("market_ticker") or msg.get("ticker") or "").strip()
        if not ticker:
            return
        yes_levels = {}
        no_levels = {}
        for level in msg.get("yes", []) or msg.get("yes_levels", []):
            if isinstance(level, dict):
                px = level.get("price")
                qty = level.get("quantity") or level.get("qty")
            else:
                try:
                    px, qty = level
                except Exception:
                    continue
            try:
                yes_levels[int(px)] = int(qty)
            except Exception:
                continue
        for level in msg.get("no", []) or msg.get("no_levels", []):
            if isinstance(level, dict):
                px = level.get("price")
                qty = level.get("quantity") or level.get("qty")
            else:
                try:
                    px, qty = level
                except Exception:
                    continue
            try:
                no_levels[int(px)] = int(qty)
            except Exception:
                continue
        state = self.orderbooks[ticker]
        state["yes"] = yes_levels
        state["no"] = no_levels
        state["seq"] = msg.get("seq")
        self._refresh_best_prices(ticker)

    def _handle_orderbook_delta(self, msg: dict[str, Any]) -> None:
        ticker = str(msg.get("market_ticker") or msg.get("ticker") or "").strip()
        if not ticker:
            return
        state = self.orderbooks[ticker]
        for side_key in ("yes", "no"):
            levels = msg.get(side_key, [])
            if not levels:
                continue
            side_book = state[side_key]
            for level in levels:
                if isinstance(level, dict):
                    px = level.get("price")
                    delta_raw = level.get("delta")
                    qty_raw = level.get("quantity") or level.get("qty")
                else:
                    try:
                        px, delta_raw = level
                        qty_raw = None
                    except Exception:
                        continue
                try:
                    price = int(px)
                    delta = int(delta_raw) if delta_raw is not None else None
                    quantity = int(qty_raw) if qty_raw is not None else None
                except Exception:
                    continue
                if delta is not None:
                    new_qty = side_book.get(price, 0) + delta
                    if new_qty <= 0:
                        side_book.pop(price, None)
                    else:
                        side_book[price] = new_qty
                    continue
                if quantity is not None:
                    if quantity <= 0:
                        side_book.pop(price, None)
                    else:
                        side_book[price] = quantity
        state["seq"] = msg.get("seq")
        self._refresh_best_prices(ticker)

    def _handle_ticker(self, msg: dict[str, Any]) -> None:
        ticker = str(msg.get("market_ticker") or msg.get("ticker") or "").strip()
        if not ticker:
            return
        state = self.orderbooks[ticker]
        yes_bid = msg.get("yes_bid")
        yes_ask = msg.get("yes_ask")
        no_bid = msg.get("no_bid")
        try:
            if yes_bid is not None:
                state["best_yes_bid"] = int(yes_bid)
            if yes_ask is not None:
                state["best_yes_ask"] = int(yes_ask)
            elif no_bid is not None:
                state["best_yes_ask"] = 100 - int(no_bid)
        except Exception:
            pass

    def _handle_lifecycle(self, msg: dict[str, Any]) -> None:
        ticker = str(msg.get("market_ticker") or msg.get("ticker") or "").strip()
        if not ticker:
            return
        for callback in self._lifecycle_callbacks:
            try:
                callback(ticker, msg)
            except Exception:
                logger.exception("kalshi_lifecycle_callback_failed ticker=%s", ticker)

    def _refresh_best_prices(self, ticker: str) -> None:
        state = self.orderbooks[ticker]
        yes_book: dict[int, int] = state.get("yes", {})
        no_book: dict[int, int] = state.get("no", {})
        yes_bid = max(yes_book.keys()) if yes_book else None
        no_bid = max(no_book.keys()) if no_book else None
        yes_ask = None if no_bid is None else (100 - no_bid)
        state["best_yes_bid"] = yes_bid
        state["best_yes_ask"] = yes_ask

    async def run(self) -> None:
        await self.manager.run()

    async def close(self) -> None:
        await self.manager.close()
