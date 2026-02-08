from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import time

from .collectors.crypto import fetch_btc_spot_ticks
from .collectors.weather import fetch_weather_ensemble_samples
from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient
from .models import MarketSnapshot
from .signals.btc import build_btc_signals
from .signals.weather import build_weather_signals

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

        inserted_weather_samples = 0
        weather_samples = []
        if self.settings.weather_enabled:
            try:
                weather_samples = fetch_weather_ensemble_samples(self.settings, now_utc=now)
                inserted_weather_samples = self.store.insert_weather_ensemble_samples(weather_samples)
            except Exception:
                logger.exception("weather_collection_failed")

        inserted_crypto_ticks = 0
        crypto_ticks = []
        if self.settings.btc_enabled:
            try:
                crypto_ticks = fetch_btc_spot_ticks(self.settings, now_utc=now)
                inserted_crypto_ticks = self.store.insert_crypto_spot_ticks(crypto_ticks)
            except Exception:
                logger.exception("btc_collection_failed")

        generated_signals = 0
        inserted_signals = 0
        try:
            snapshots_by_ticker = {snapshot.ticker: snapshot for snapshot in current_snapshots}
            all_signals = []
            if weather_samples:
                all_signals.extend(
                    build_weather_signals(
                        self.settings,
                        markets,
                        snapshots_by_ticker,
                        weather_samples,
                        now_utc=now,
                    )
                )
            if self.settings.btc_enabled:
                lookback_window = max(self.settings.btc_momentum_lookback_minutes + 2, 20)
                recent_ticks = self.store.get_recent_crypto_spot_ticks(
                    symbol=self.settings.btc_symbol,
                    since_ts=now - timedelta(minutes=lookback_window),
                )
                all_signals.extend(
                    build_btc_signals(
                        self.settings,
                        markets,
                        snapshots_by_ticker,
                        recent_ticks,
                        crypto_ticks,
                        now_utc=now,
                    )
                )
            generated_signals = len(all_signals)
            if all_signals:
                inserted_signals = self.store.insert_signals(all_signals)
        except Exception:
            logger.exception("signal_generation_failed")

        return {
            "markets_seen": len(markets),
            "current_snapshots_inserted": inserted_current,
            "historical_snapshots_inserted": inserted_historical,
            "current_snapshot_failures": failed_markets,
            "weather_samples_inserted": inserted_weather_samples,
            "crypto_ticks_inserted": inserted_crypto_ticks,
            "signals_generated": generated_signals,
            "signals_inserted": inserted_signals,
        }

    def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                stats = self.run_once()
                metrics = " ".join(f"{key}={value}" for key, value in stats.items())
                logger.info("poll_complete %s", metrics)
            except Exception:
                logger.exception("poll_failed")
            elapsed = time.monotonic() - started
            sleep_seconds = max(1, self.settings.poll_interval_seconds - int(elapsed))
            time.sleep(sleep_seconds)
