from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings
from .kalshi_client import KalshiClient
from .pipeline import DataPipeline
from .ws.binance_feed import BinanceFeed
from .ws.coinbase_feed import CoinbaseFeed
from .ws.kalshi_feed import KalshiFeed, build_kalshi_ws_url
from .ws.kraken_feed import KrakenFeed

logger = logging.getLogger(__name__)


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

        try:
            self.kalshi_feed = KalshiFeed(
                client=self.client,
                ws_url=build_kalshi_ws_url(self.settings.kalshi_base_url),
            )
        except Exception:
            logger.exception("kalshi_ws_init_failed")
            self.kalshi_feed = None

    async def bootstrap_subscriptions(self) -> None:
        if self.kalshi_feed is None:
            return
        try:
            markets = await asyncio.to_thread(
                self.client.list_markets, self.settings.market_limit
            )
            tickers = [market.ticker for market in markets if market.ticker]
            if tickers:
                await self.kalshi_feed.subscribe_market(tickers[0])
                for ticker in tickers[1:]:
                    await self.kalshi_feed.subscribe_market(ticker)
            await self.kalshi_feed.subscribe_lifecycle()
        except Exception:
            logger.exception("kalshi_ws_subscribe_failed")

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
