from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import time
from typing import Any

from .config import Settings
from .kalshi_client import KalshiClient
from .pipeline import DataPipeline
from .ws.binance_feed import BinanceFeed
from .ws.coinbase_feed import CoinbaseFeed
from .ws.kalshi_feed import KalshiFeed, build_kalshi_ws_url
from .ws.kraken_feed import KrakenFeed

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class AsyncRuntime:
    def __init__(self, settings: Settings, pipeline: DataPipeline, client: KalshiClient) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.client = client
        self.kalshi_feed: KalshiFeed | None = None
        self.binance_feed = BinanceFeed()
        self.coinbase_feed = CoinbaseFeed()
        self.kraken_feed = KrakenFeed()
        self._running = True
        self._subscribed_tickers: set[str] = set()
        self._lifecycle_queue: asyncio.Queue[str] = asyncio.Queue()

        try:
            self.kalshi_feed = KalshiFeed(
                client=self.client,
                ws_url=build_kalshi_ws_url(self.settings.kalshi_base_url),
            )
            self.kalshi_feed.add_lifecycle_callback(self._on_lifecycle_market)
        except Exception:
            logger.exception("kalshi_ws_init_failed")
            self.kalshi_feed = None

    def _on_lifecycle_market(self, ticker: str, _payload: dict[str, Any]) -> None:
        if not ticker:
            return
        normalized = ticker.strip().upper()
        if not normalized:
            return
        # Prioritize auto-subscribe for new BTC 15m contracts.
        if not normalized.startswith("KXBTC15M"):
            return
        if normalized in self._subscribed_tickers:
            return
        try:
            self._lifecycle_queue.put_nowait(normalized)
        except asyncio.QueueFull:
            logger.warning("kalshi_lifecycle_queue_full ticker=%s", normalized)

    async def bootstrap_subscriptions(self) -> None:
        if self.kalshi_feed is None:
            return
        try:
            markets = await asyncio.to_thread(
                self.client.list_markets, self.settings.market_limit
            )
            tickers = [market.ticker.strip().upper() for market in markets if market.ticker]
            if tickers:
                await self.kalshi_feed.subscribe_market(tickers[0])
                self._subscribed_tickers.add(tickers[0])
                for ticker in tickers[1:]:
                    await self.kalshi_feed.subscribe_market(ticker)
                    self._subscribed_tickers.add(ticker)
            await self.kalshi_feed.subscribe_lifecycle()
        except Exception:
            logger.exception("kalshi_ws_subscribe_failed")

    async def lifecycle_subscriber_loop(self) -> None:
        if self.kalshi_feed is None:
            return
        while self._running:
            try:
                ticker = await asyncio.wait_for(self._lifecycle_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            try:
                if ticker in self._subscribed_tickers:
                    continue
                await self.kalshi_feed.subscribe_market(ticker)
                self._subscribed_tickers.add(ticker)
                logger.info("kalshi_ws_auto_subscribed ticker=%s", ticker)
            except Exception:
                logger.exception("kalshi_ws_auto_subscribe_failed ticker=%s", ticker)

    def _rest_best_yes_bid_ask(self, market_payload: dict[str, Any]) -> tuple[int | None, int | None]:
        market = market_payload.get("market", market_payload)
        if not isinstance(market, dict):
            return None, None
        yes_bid = _as_int(market.get("yes_bid"))
        yes_ask = _as_int(market.get("yes_ask"))
        if yes_ask is None:
            no_bid = _as_int(market.get("no_bid"))
            if no_bid is not None:
                yes_ask = 100 - no_bid
        return yes_bid, yes_ask

    async def ws_rest_health_loop(self) -> None:
        if self.kalshi_feed is None:
            return
        while self._running:
            await asyncio.sleep(60)
            if not self._subscribed_tickers:
                continue
            tickers = list(sorted(self._subscribed_tickers))[:10]
            alerts: list[str] = []
            for ticker in tickers:
                try:
                    payload = await asyncio.to_thread(
                        self.client._request_json,  # noqa: SLF001
                        "GET",
                        f"/trade-api/v2/markets/{ticker}",
                    )
                except Exception:
                    logger.warning("ws_rest_health_fetch_failed ticker=%s", ticker, exc_info=True)
                    continue

                ws_yes_bid, ws_yes_ask = self.kalshi_feed.get_best_bid_ask(ticker)
                rest_yes_bid, rest_yes_ask = self._rest_best_yes_bid_ask(payload)

                if ws_yes_bid is None and ws_yes_ask is None:
                    alerts.append(f"⚠️ WS orderbook missing for {ticker} (REST available).")
                    continue

                bid_diff = (
                    abs(ws_yes_bid - rest_yes_bid)
                    if ws_yes_bid is not None and rest_yes_bid is not None
                    else None
                )
                ask_diff = (
                    abs(ws_yes_ask - rest_yes_ask)
                    if ws_yes_ask is not None and rest_yes_ask is not None
                    else None
                )
                if (bid_diff is not None and bid_diff > 2) or (
                    ask_diff is not None and ask_diff > 2
                ):
                    alerts.append(
                        "⚠️ WS/REST divergence "
                        f"{ticker}: ws_bid={ws_yes_bid} rest_bid={rest_yes_bid} "
                        f"ws_ask={ws_yes_ask} rest_ask={rest_yes_ask}"
                    )

            if not alerts:
                continue
            now = datetime.now(timezone.utc)
            filtered = self.pipeline._filter_operational_alerts(now, alerts)  # noqa: SLF001
            if not filtered:
                continue
            events = await asyncio.to_thread(
                self.pipeline.telegram_notifier.notify_operational_alerts, now, filtered
            )
            if events:
                await asyncio.to_thread(self.pipeline.store.insert_alert_events, events)

    async def periodic_poll_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                stats = await asyncio.to_thread(self.pipeline.run_once)
                metrics = " ".join(f"{key}={value}" for key, value in stats.items())
                logger.info("poll_complete %s", metrics)
            except Exception:
                logger.exception("poll_failed")

            elapsed = time.monotonic() - started
            sleep_seconds = max(1, self.settings.poll_interval_seconds - int(elapsed))
            await asyncio.sleep(sleep_seconds)

    async def command_poll_loop(self) -> None:
        while self._running:
            try:
                events = await asyncio.to_thread(
                    self.pipeline.telegram_notifier.poll_commands, self.pipeline
                )
                if events:
                    await asyncio.to_thread(self.pipeline.store.insert_alert_events, events)
            except Exception:
                logger.exception("telegram_command_poll_failed")
            await asyncio.sleep(2)

    async def run(self) -> None:
        await self.bootstrap_subscriptions()
        tasks = [
            asyncio.create_task(self.periodic_poll_loop()),
            asyncio.create_task(self.command_poll_loop()),
            asyncio.create_task(self.lifecycle_subscriber_loop()),
            asyncio.create_task(self.ws_rest_health_loop()),
            asyncio.create_task(self.binance_feed.run()),
            asyncio.create_task(self.coinbase_feed.run()),
            asyncio.create_task(self.kraken_feed.run()),
        ]
        if self.kalshi_feed is not None:
            tasks.append(asyncio.create_task(self.kalshi_feed.run()))
        try:
            await asyncio.gather(*tasks)
        finally:
            self._running = False
            for task in tasks:
                task.cancel()
            await self.binance_feed.close()
            await self.coinbase_feed.close()
            await self.kraken_feed.close()
            if self.kalshi_feed is not None:
                await self.kalshi_feed.close()
