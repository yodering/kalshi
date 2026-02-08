from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import time

from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient
from .models import MarketSnapshot

logger = logging.getLogger(__name__)


class DataPipeline:
    def __init__(self, settings: Settings, client: KalshiClient, store: PostgresStore) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.did_backfill = False

    def run_once(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        markets = self.client.list_markets(self.settings.market_limit)
        if not markets:
            logger.warning(
                "No markets matched current target filters. Check TARGET_* env settings."
            )
            return {
                "markets_seen": 0,
                "current_snapshots_inserted": 0,
                "historical_snapshots_inserted": 0,
                "current_snapshot_failures": 0,
            }
        logger.info("target_markets %s", ",".join(market.ticker for market in markets))
        ticker_to_id = self.store.upsert_markets(markets)

        current_snapshots: list[MarketSnapshot] = []
        failed_markets = 0
        for market in markets:
            try:
                current_snapshots.append(self.client.get_current_snapshot(market))
            except Exception:
                failed_markets += 1
                logger.exception("Failed current snapshot for ticker=%s", market.ticker)
        inserted_current = self.store.insert_snapshots(current_snapshots, ticker_to_id)

        inserted_historical = 0
        if self.settings.run_historical_backfill_on_start and not self.did_backfill:
            start = now - timedelta(days=self.settings.historical_days)
            for market in markets[: self.settings.historical_markets]:
                try:
                    history = self.client.get_historical_snapshots(market, start, now)
                except Exception:
                    logger.exception("Failed historical fetch for ticker=%s", market.ticker)
                    continue
                inserted_historical += self.store.insert_snapshots(history, ticker_to_id)
            self.did_backfill = True

        return {
            "markets_seen": len(markets),
            "current_snapshots_inserted": inserted_current,
            "historical_snapshots_inserted": inserted_historical,
            "current_snapshot_failures": failed_markets,
        }

    def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                stats = self.run_once()
                logger.info(
                    "poll_complete markets=%s current_inserted=%s historical_inserted=%s failures=%s",
                    stats["markets_seen"],
                    stats["current_snapshots_inserted"],
                    stats["historical_snapshots_inserted"],
                    stats["current_snapshot_failures"],
                )
            except Exception:
                logger.exception("poll_failed")
            elapsed = time.monotonic() - started
            sleep_seconds = max(1, self.settings.poll_interval_seconds - int(elapsed))
            time.sleep(sleep_seconds)
