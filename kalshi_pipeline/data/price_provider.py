from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from ..db import PostgresStore
from ..kalshi_client import KalshiClient
from ..models import MarketSnapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceSnapshot:
    price: float
    timestamp: datetime
    source: str


class PriceProvider:
    """WS-first data accessor with DB/REST fallback."""

    def __init__(
        self,
        *,
        binance_feed: Any | None,
        coinbase_feed: Any | None,
        kraken_feed: Any | None,
        kalshi_feed: Any | None,
        store: PostgresStore,
        client: KalshiClient,
        btc_symbol: str = "BTCUSD",
    ) -> None:
        self._binance = binance_feed
        self._coinbase = coinbase_feed
        self._kraken = kraken_feed
        self._kalshi = kalshi_feed
        self._store = store
        self._client = client
        self._btc_symbol = btc_symbol

    def get_btc_prices(self) -> dict[str, PriceSnapshot]:
        prices: dict[str, PriceSnapshot] = {}
        ws_sources = [
            ("binance", self._binance),
            ("coinbase", self._coinbase),
            ("kraken", self._kraken),
        ]
        for source_name, feed in ws_sources:
            if feed is not None and getattr(feed, "is_connected", False):
                age_seconds = float(getattr(feed, "age_seconds", float("inf")))
                latest_price = feed.get_latest_price() if hasattr(feed, "get_latest_price") else None
                latest_ts = getattr(feed, "last_update_time", None)
                if latest_price is not None and latest_ts is not None and age_seconds < 5.0:
                    prices[source_name] = PriceSnapshot(
                        price=float(latest_price),
                        timestamp=latest_ts,
                        source="ws",
                    )
                    continue

            fallback = self._store.get_latest_spot_tick(source=source_name, symbol=self._btc_symbol)
            if not fallback:
                continue
            age_seconds = float(fallback.get("age_seconds") or 0.0)
            if age_seconds > 30.0:
                continue
            ts = fallback.get("ts")
            if not isinstance(ts, datetime):
                continue
            prices[source_name] = PriceSnapshot(
                price=float(fallback["price_usd"]),
                timestamp=ts,
                source="rest_fallback",
            )
        return prices

    def get_btc_momentum(self, window_seconds: int = 300) -> float | None:
        if self._binance is not None and getattr(self._binance, "is_connected", False):
            if hasattr(self._binance, "get_price_history_window"):
                history = self._binance.get_price_history_window(window_seconds)
            else:
                history = []
            if len(history) >= 2 and history[0] > 0:
                return (history[-1] - history[0]) / history[0]

        since_ts = datetime.now(timezone.utc) - timedelta(seconds=max(10, window_seconds))
        ticks = self._store.get_recent_crypto_spot_ticks(self._btc_symbol, since_ts)
        prices = [tick.price_usd for tick in ticks if tick.price_usd > 0]
        if len(prices) < 2:
            return None
        return (prices[-1] - prices[0]) / prices[0]

    def get_kalshi_orderbook(self, ticker: str) -> dict[str, Any] | None:
        cleaned_ticker = ticker.strip().upper()
        if not cleaned_ticker:
            return None
        if self._kalshi is not None and getattr(self._kalshi, "is_connected", False):
            try:
                has_book = self._kalshi.has_orderbook(cleaned_ticker)
                age_seconds = self._kalshi.get_orderbook_age_seconds(cleaned_ticker)
                if has_book and (age_seconds is None or age_seconds <= 10.0):
                    return self._kalshi.get_orderbook(cleaned_ticker)
            except Exception:
                logger.warning("price_provider_kalshi_ws_orderbook_failed", exc_info=True)
        try:
            return self._client.get_orderbook(cleaned_ticker)
        except Exception:
            logger.warning(
                "price_provider_kalshi_rest_orderbook_failed ticker=%s",
                cleaned_ticker,
                exc_info=True,
            )
            return None

    def get_market_snapshot(self, ticker: str) -> MarketSnapshot | None:
        cleaned_ticker = ticker.strip().upper()
        if not cleaned_ticker:
            return None
        now_utc = datetime.now(timezone.utc)
        book = self.get_kalshi_orderbook(cleaned_ticker)
        if book:
            yes_levels = book.get("yes") or []
            no_levels = book.get("no") or []
            yes_bid = max((int(level[0]) for level in yes_levels), default=None)
            no_bid = max((int(level[0]) for level in no_levels), default=None)
            yes_ask = None if no_bid is None else (100 - no_bid)
            no_ask = None if yes_bid is None else (100 - yes_bid)
            yes_price = (yes_ask / 100.0) if yes_ask is not None else None
            no_price = (no_ask / 100.0) if no_ask is not None else None
            return MarketSnapshot(
                ticker=cleaned_ticker,
                ts=now_utc,
                yes_price=yes_price,
                no_price=no_price,
                volume=None,
                raw_json={
                    "source": book.get("source", "ws"),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "orderbook": book,
                },
            )
        try:
            payload = self._client._request_json(  # noqa: SLF001
                "GET",
                f"/trade-api/v2/markets/{cleaned_ticker}",
            )
            market = payload.get("market", payload) if isinstance(payload, dict) else {}
            yes_ask = market.get("yes_ask")
            yes_bid = market.get("yes_bid")
            no_bid = market.get("no_bid")
            no_ask = market.get("no_ask")
            yes_price = None
            no_price = None
            if yes_ask is not None:
                yes_price = float(yes_ask) / 100.0 if float(yes_ask) > 1 else float(yes_ask)
            elif yes_bid is not None:
                yes_price = float(yes_bid) / 100.0 if float(yes_bid) > 1 else float(yes_bid)
            if no_ask is not None:
                no_price = float(no_ask) / 100.0 if float(no_ask) > 1 else float(no_ask)
            elif no_bid is not None:
                no_price = float(no_bid) / 100.0 if float(no_bid) > 1 else float(no_bid)
            return MarketSnapshot(
                ticker=cleaned_ticker,
                ts=now_utc,
                yes_price=yes_price,
                no_price=no_price,
                volume=None,
                raw_json=market if isinstance(market, dict) else {},
            )
        except Exception:
            logger.warning(
                "price_provider_market_snapshot_failed ticker=%s",
                cleaned_ticker,
                exc_info=True,
            )
            return None
