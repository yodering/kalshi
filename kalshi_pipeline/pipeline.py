from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any

from .collectors.crypto import fetch_btc_spot_ticks
from .collectors.resolutions import collect_market_resolutions
from .collectors.weather import fetch_weather_ensemble_samples
from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient
from .models import MarketSnapshot
from .notifications import TelegramNotifier
from .paper_trading import PaperTradingEngine
from .signals.btc import build_btc_signals
from .signals.edge_monitor import build_edge_decay_alerts
from .signals.weather import build_weather_probabilities, build_weather_signals

logger = logging.getLogger(__name__)


class DataPipeline:
    def __init__(self, settings: Settings, client: KalshiClient, store: PostgresStore) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.did_backfill = False
        self.paper_trader = PaperTradingEngine(settings, client, store)
        self.telegram_notifier = TelegramNotifier(settings)
        self.paused = False
        self.runtime_mode = settings.bot_mode
        self.runtime_auto_trading_enabled = settings.paper_trading_enabled
        self.pending_live_mode: str | None = None
        self.last_poll_at: datetime | None = None
        self.last_stats: dict[str, int] = {}

    def set_paused(self, paused: bool) -> None:
        self.paused = paused

    def get_runtime_status(self) -> dict[str, object]:
        return {
            "mode": self.runtime_mode,
            "paused": self.paused,
            "last_poll_at": self.last_poll_at.isoformat() if self.last_poll_at else None,
            "last_metrics": self.last_stats,
        }

    def request_mode_change(self, requested_mode: str) -> str:
        mode = requested_mode.strip().lower()
        if mode not in {"custom", "demo_safe", "live_safe", "live_auto"}:
            return "Unsupported mode. Use one of: custom, demo_safe, live_safe, live_auto."
        if mode in {"live_safe", "live_auto"}:
            self.pending_live_mode = mode
            return (
                "⚠️ Live mode requested. Type `CONFIRM LIVE` to apply. "
                "This will affect auto-trading behavior."
            )
        self.pending_live_mode = None
        self.runtime_mode = mode
        if mode == "custom":
            self.runtime_auto_trading_enabled = self.settings.paper_trading_enabled
        elif mode == "demo_safe":
            self.runtime_auto_trading_enabled = True
        return (
            f"Mode changed to {self.runtime_mode}. "
            f"auto_trading={'on' if self.runtime_auto_trading_enabled else 'off'}."
        )

    def confirm_live_mode(self) -> str:
        if self.pending_live_mode is None:
            return "No pending live mode change."
        mode = self.pending_live_mode
        self.pending_live_mode = None
        self.runtime_mode = mode
        self.runtime_auto_trading_enabled = mode == "live_auto"
        return (
            f"✅ Mode changed to {mode}. "
            f"auto_trading={'on' if self.runtime_auto_trading_enabled else 'off'}."
        )

    def get_balance_snapshot(self) -> str | None:
        if self.settings.paper_trading_mode != "kalshi_demo":
            return None
        try:
            payload = self.client._request_json(  # noqa: SLF001
                "GET",
                "/trade-api/v2/portfolio/balance",
                require_auth=True,
                base_url_override=self.settings.paper_trading_base_url,
            )
        except Exception:
            logger.exception("balance_fetch_failed")
            return None
        if not isinstance(payload, dict):
            return str(payload)
        return ", ".join(f"{key}={value}" for key, value in payload.items())

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
                "weather_samples_inserted": 0,
                "weather_forecasts_inserted": 0,
                "weather_bracket_probs_inserted": 0,
                "crypto_ticks_inserted": 0,
                "signals_generated": 0,
                "signals_inserted": 0,
                "paper_orders_candidates": 0,
                "paper_orders_attempted": 0,
                "paper_orders_submitted": 0,
                "paper_orders_simulated": 0,
                "paper_orders_failed": 0,
                "paper_orders_skipped": 0,
                "paper_orders_recorded": 0,
                "alert_events_inserted": 0,
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
        inserted_weather_forecasts = 0
        inserted_weather_bracket_probs = 0
        weather_samples = []
        if self.settings.weather_enabled:
            try:
                weather_samples = fetch_weather_ensemble_samples(self.settings, now_utc=now)
                inserted_weather_samples = self.store.insert_weather_ensemble_samples(weather_samples)
                inserted_weather_forecasts = self.store.insert_weather_ensemble_forecasts(
                    weather_samples
                )
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
        all_signals = []
        try:
            snapshots_by_ticker = {snapshot.ticker: snapshot for snapshot in current_snapshots}
            if weather_samples:
                weather_prob_rows = build_weather_probabilities(
                    markets,
                    snapshots_by_ticker,
                    weather_samples,
                    now_utc=now,
                )
                if weather_prob_rows:
                    inserted_weather_bracket_probs = self.store.insert_weather_bracket_probabilities(
                        weather_prob_rows
                    )
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
            snapshots_by_ticker = {snapshot.ticker: snapshot for snapshot in current_snapshots}

        # Resolution tracking and accuracy materialization run opportunistically.
        try:
            resolution_rows = collect_market_resolutions(
                self.client,
                [market.ticker for market in markets],
                target_series_tickers=self.settings.target_series_tickers,
                base_url_override=self.settings.paper_trading_base_url
                if self.settings.paper_trading_mode == "kalshi_demo"
                else None,
                now_utc=now,
            )
            if resolution_rows:
                self.store.upsert_market_resolutions(resolution_rows)
            self.store.materialize_prediction_accuracy()
        except Exception:
            logger.exception("resolution_tracking_failed")

        paper_stats = {
            "paper_orders_candidates": 0,
            "paper_orders_attempted": 0,
            "paper_orders_submitted": 0,
            "paper_orders_simulated": 0,
            "paper_orders_failed": 0,
            "paper_orders_skipped": 0,
            "paper_orders_recorded": 0,
        }
        paper_orders = []
        try:
            if self.paused or not self.runtime_auto_trading_enabled:
                logger.info(
                    "paper_trading_skipped paused=%s runtime_auto_trading_enabled=%s",
                    self.paused,
                    self.runtime_auto_trading_enabled,
                )
            else:
                paper_orders, paper_stats = self.paper_trader.execute(
                    all_signals, snapshots_by_ticker, now
                )
        except Exception:
            logger.exception("paper_trading_failed")

        alert_events_inserted = 0
        try:
            alert_events = self.telegram_notifier.notify(now, all_signals, paper_orders)
            open_positions = self.store.get_open_positions_summary()
            signal_rows = [
                {
                    "market_ticker": signal.market_ticker,
                    "direction": signal.direction,
                    "edge_bps": signal.edge_bps,
                }
                for signal in all_signals
                if signal.market_ticker is not None
            ]
            decay_messages = build_edge_decay_alerts(
                open_positions=open_positions,
                current_signals=signal_rows,
                edge_decay_alert_threshold_bps=self.settings.edge_decay_alert_threshold_bps,
            )
            if decay_messages:
                alert_events.extend(
                    self.telegram_notifier.notify_operational_alerts(now, decay_messages)
                )
            if alert_events:
                alert_events_inserted = self.store.insert_alert_events(alert_events)
        except Exception:
            logger.exception("alerting_failed")

        stats = {
            "markets_seen": len(markets),
            "current_snapshots_inserted": inserted_current,
            "historical_snapshots_inserted": inserted_historical,
            "current_snapshot_failures": failed_markets,
            "weather_samples_inserted": inserted_weather_samples,
            "weather_forecasts_inserted": inserted_weather_forecasts,
            "weather_bracket_probs_inserted": inserted_weather_bracket_probs,
            "crypto_ticks_inserted": inserted_crypto_ticks,
            "signals_generated": generated_signals,
            "signals_inserted": inserted_signals,
            "alert_events_inserted": alert_events_inserted,
        }
        stats.update(paper_stats)
        self.last_poll_at = now
        self.last_stats = stats
        return stats

    def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            command_events: list[Any] = []
            try:
                command_events = self.telegram_notifier.poll_commands(self)
                if command_events:
                    self.store.insert_alert_events(command_events)
            except Exception:
                logger.exception("telegram_command_poll_failed")
            try:
                stats = self.run_once()
                metrics = " ".join(f"{key}={value}" for key, value in stats.items())
                logger.info("poll_complete %s", metrics)
            except Exception:
                logger.exception("poll_failed")
            elapsed = time.monotonic() - started
            remaining = max(1, self.settings.poll_interval_seconds - int(elapsed))
            while remaining > 0:
                sleep_chunk = min(2, remaining)
                time.sleep(sleep_chunk)
                remaining -= sleep_chunk
                try:
                    command_events = self.telegram_notifier.poll_commands(self)
                    if command_events:
                        self.store.insert_alert_events(command_events)
                except Exception:
                    logger.exception("telegram_command_poll_failed")
