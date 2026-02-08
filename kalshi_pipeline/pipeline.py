from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import time
from typing import TYPE_CHECKING, Any

from .collectors.crypto import fetch_btc_spot_ticks
from .collectors.resolutions import collect_market_resolutions
from .collectors.weather import fetch_weather_ensemble_samples
from .analysis.weather_backtest import check_weather_live_gates, generate_weather_calibration
from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient
from .models import CryptoSpotTick, Market, MarketSnapshot
from .notifications import TelegramNotifier
from .paper_trading import PaperTradingEngine
from .signals.bracket_arb import BracketArbOpportunity, scan_bracket_arbitrage
from .signals.btc import build_btc_signals
from .signals.edge_monitor import build_edge_decay_alerts
from .signals.weather import build_weather_probabilities, build_weather_signals

if TYPE_CHECKING:
    from .data.price_provider import PriceProvider

logger = logging.getLogger(__name__)


class DataPipeline:
    def __init__(self, settings: Settings, client: KalshiClient, store: PostgresStore) -> None:
        self.settings = settings
        self.client = client
        self.store = store
        self.price_provider: PriceProvider | None = None
        self._last_markets: list[Market] = []
        self.did_backfill = False
        self.paper_trader = PaperTradingEngine(settings, client, store)
        self.telegram_notifier = TelegramNotifier(settings)
        self.paused = False
        self.runtime_mode = settings.bot_mode
        self.runtime_auto_trading_enabled = settings.paper_trading_enabled
        self.pending_live_mode: str | None = None
        self.last_poll_at: datetime | None = None
        self.last_stats: dict[str, int] = {}
        self._operational_alert_last_sent_at: dict[str, datetime] = {}
        self._operational_alert_cooldown = timedelta(hours=6)
        self._operational_alert_max_per_cycle = 3

    def set_price_provider(self, price_provider: "PriceProvider") -> None:
        self.price_provider = price_provider

    @staticmethod
    def _is_btc_market(market: Market) -> bool:
        ticker = market.ticker.upper()
        if ticker.startswith("KXBTC15M"):
            return True
        return str(market.raw_json.get("series_ticker", "")).upper() == "KXBTC15M"

    @staticmethod
    def _is_weather_market(market: Market) -> bool:
        ticker = market.ticker.upper()
        if ticker.startswith("KXHIGHNY"):
            return True
        return str(market.raw_json.get("series_ticker", "")).upper() == "KXHIGHNY"

    @staticmethod
    def _event_key_for_market(market: Market) -> str:
        raw = market.raw_json if isinstance(market.raw_json, dict) else {}
        event = str(raw.get("event_ticker") or raw.get("event") or "").strip().upper()
        if event:
            return event
        ticker = market.ticker.strip().upper()
        if "-" in ticker:
            return ticker.split("-", 1)[0]
        return ticker

    def _get_orderbook(self, ticker: str) -> dict[str, Any] | None:
        if self.price_provider is not None:
            try:
                orderbook = self.price_provider.get_kalshi_orderbook(ticker)
                if orderbook:
                    return orderbook
            except Exception:
                logger.warning("orderbook_fetch_failed ticker=%s source=price_provider", ticker, exc_info=True)
        try:
            return self.client.get_orderbook(ticker)
        except Exception:
            logger.warning("orderbook_fetch_failed ticker=%s source=rest", ticker, exc_info=True)
            return None

    def _scan_bracket_arbitrage(
        self,
        *,
        markets: list[Market],
        orderbooks_by_ticker: dict[str, dict[str, Any]],
        now_utc: datetime,
    ) -> list[BracketArbOpportunity]:
        if not self.settings.bracket_arb_enabled:
            return []

        grouped_events: dict[str, list[str]] = {}
        for market in markets:
            if not self._is_weather_market(market):
                continue
            event_key = self._event_key_for_market(market)
            grouped_events.setdefault(event_key, []).append(market.ticker)

        opportunities: list[BracketArbOpportunity] = []
        for event_ticker, tickers in grouped_events.items():
            if len(tickers) < 2:
                continue
            if any(ticker not in orderbooks_by_ticker for ticker in tickers):
                continue
            opportunity = scan_bracket_arbitrage(
                event_ticker=event_ticker,
                bracket_tickers=tickers,
                orderbooks=orderbooks_by_ticker,
                min_profit_after_fees_cents=self.settings.bracket_arb_min_profit_after_fees_cents,
                now_utc=now_utc,
            )
            if opportunity is None:
                continue
            opportunities.append(opportunity)
        opportunities.sort(key=lambda row: row.profit_after_fees_cents, reverse=True)
        return opportunities

    def _filter_operational_alerts(self, now_utc: datetime, messages: list[str]) -> list[str]:
        if not messages:
            return []
        filtered: list[str] = []
        seen_this_cycle: set[str] = set()

        # Garbage-collect old keys so this map does not grow forever.
        gc_before = now_utc - timedelta(days=2)
        stale_keys = [
            key
            for key, ts in self._operational_alert_last_sent_at.items()
            if ts < gc_before
        ]
        for key in stale_keys:
            self._operational_alert_last_sent_at.pop(key, None)

        for message in messages:
            key = message.strip()
            if not key or key in seen_this_cycle:
                continue
            seen_this_cycle.add(key)
            last_sent_at = self._operational_alert_last_sent_at.get(key)
            if last_sent_at is not None and (now_utc - last_sent_at) < self._operational_alert_cooldown:
                continue
            filtered.append(message)
            self._operational_alert_last_sent_at[key] = now_utc
            if len(filtered) >= self._operational_alert_max_per_cycle:
                break
        return filtered

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
        self._last_markets = list(markets)
        resolution_rows_upserted = 0
        prediction_accuracy_rows_materialized = 0
        if not markets:
            logger.warning(
                "No markets matched current target filters. Check TARGET_* env settings."
            )
            try:
                resolution_rows = collect_market_resolutions(
                    self.client,
                    [],
                    target_series_tickers=self.settings.target_series_tickers,
                    base_url_override=self.settings.paper_trading_base_url
                    if self.settings.paper_trading_mode == "kalshi_demo"
                    else None,
                    now_utc=now,
                )
                if resolution_rows:
                    resolution_rows_upserted = self.store.upsert_market_resolutions(
                        resolution_rows
                    )
                prediction_accuracy_rows_materialized = (
                    self.store.materialize_prediction_accuracy()
                )
            except Exception:
                logger.exception("resolution_tracking_failed")
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
                "paper_order_events_inserted": 0,
                "paper_orders_status_updates": 0,
                "paper_orders_filled": 0,
                "paper_orders_canceled": 0,
                "paper_orders_failed_reconcile": 0,
                "paper_orders_repriced": 0,
                "paper_orders_reprice_recorded": 0,
                "paper_orders_reprice_failed": 0,
                "paper_orders_queue_alerted": 0,
                "alert_events_inserted": 0,
                "resolutions_upserted": resolution_rows_upserted,
                "prediction_accuracy_materialized": prediction_accuracy_rows_materialized,
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
        detected_arb_opportunities: list[BracketArbOpportunity] = []
        serialized_arb_rows: list[dict[str, Any]] = []
        inserted_arb_opportunities = 0
        try:
            snapshots_by_ticker = {snapshot.ticker: snapshot for snapshot in current_snapshots}
            orderbooks_by_ticker: dict[str, dict[str, Any]] = {}
            for market in markets:
                orderbook = self._get_orderbook(market.ticker)
                if isinstance(orderbook, dict):
                    orderbooks_by_ticker[market.ticker] = orderbook
            detected_arb_opportunities = self._scan_bracket_arbitrage(
                markets=markets,
                orderbooks_by_ticker=orderbooks_by_ticker,
                now_utc=now,
            )
            for opportunity in detected_arb_opportunities:
                serialized_arb_rows.append(
                    {
                        "detected_at": opportunity.detected_at,
                        "event_ticker": opportunity.event_ticker,
                        "arb_type": opportunity.arb_type,
                        "n_brackets": len(opportunity.legs),
                        "cost_cents": opportunity.cost_cents,
                        "payout_cents": opportunity.payout_cents,
                        "profit_cents": opportunity.profit_cents,
                        "profit_after_fees_cents": opportunity.profit_after_fees_cents,
                        "max_sets": opportunity.max_sets,
                        "total_profit_cents": opportunity.total_profit_cents,
                        "legs": opportunity.legs,
                        "executed": False,
                        "execution_result": {},
                    }
                )
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
                        orderbooks_by_ticker=orderbooks_by_ticker,
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
                        price_provider=self.price_provider,
                        orderbooks_by_ticker=orderbooks_by_ticker,
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
                resolution_rows_upserted = self.store.upsert_market_resolutions(resolution_rows)
            prediction_accuracy_rows_materialized = self.store.materialize_prediction_accuracy()
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
            "paper_order_events_inserted": 0,
            "paper_orders_status_updates": 0,
            "paper_orders_filled": 0,
            "paper_orders_canceled": 0,
            "paper_orders_failed_reconcile": 0,
            "paper_orders_repriced": 0,
            "paper_orders_reprice_recorded": 0,
            "paper_orders_reprice_failed": 0,
            "paper_orders_queue_alerted": 0,
            "arb_opportunities_detected": len(detected_arb_opportunities),
            "arb_opportunities_inserted": 0,
            "weather_gate_blocked": 0,
        }
        paper_orders = []
        repriced_orders = []
        arb_execution_results: list[dict[str, Any]] = []
        try:
            executable_signals = list(all_signals)
            if self.runtime_mode in {"live_safe", "live_auto"}:
                calibration_report = generate_weather_calibration(
                    self.store,
                    days=max(30, self.settings.weather_live_gate_min_resolved_days),
                )
                gates = check_weather_live_gates(calibration_report, self.settings)
                if not all(gates.values()):
                    executable_signals = [
                        signal for signal in executable_signals if signal.signal_type != "weather"
                    ]
                    paper_stats["weather_gate_blocked"] = 1

            if self.paused or not self.runtime_auto_trading_enabled:
                logger.info(
                    "paper_trading_skipped paused=%s runtime_auto_trading_enabled=%s",
                    self.paused,
                    self.runtime_auto_trading_enabled,
                )
            else:
                paper_orders, paper_stats, arb_execution_results = self.paper_trader.execute(
                    executable_signals,
                    snapshots_by_ticker,
                    now,
                    arb_opportunities=serialized_arb_rows,
                )

            if self.settings.paper_trading_mode == "kalshi_demo":
                repriced_orders, reconcile_stats = self.paper_trader.reconcile_open_orders(
                    signals=all_signals,
                    snapshots_by_ticker=snapshots_by_ticker,
                    now_utc=now,
                    allow_reprice=(not self.paused and self.runtime_auto_trading_enabled),
                )
                if repriced_orders:
                    paper_orders.extend(repriced_orders)
                for key, value in reconcile_stats.items():
                    paper_stats[key] = paper_stats.get(key, 0) + value
        except Exception:
            logger.exception("paper_trading_failed")

        if serialized_arb_rows:
            result_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for result in arb_execution_results:
                event_ticker = str(result.get("event_ticker") or "")
                arb_type = str(result.get("arb_type") or "")
                if not event_ticker or not arb_type:
                    continue
                result_by_key[(event_ticker, arb_type)] = result
            for row in serialized_arb_rows:
                key = (str(row.get("event_ticker") or ""), str(row.get("arb_type") or ""))
                result = result_by_key.get(key)
                if result is None:
                    continue
                row["executed"] = bool(result.get("executed"))
                row["execution_result"] = result
            try:
                inserted_arb_ids = self.store.insert_bracket_arb_opportunities(serialized_arb_rows)
                inserted_arb_opportunities = len(inserted_arb_ids)
            except Exception:
                logger.exception("arb_persist_failed")
            paper_stats["arb_opportunities_inserted"] = inserted_arb_opportunities

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
                active_market_tickers={market.ticker for market in markets},
            )
            arb_messages: list[str] = []
            for opportunity in detected_arb_opportunities[:3]:
                arb_messages.append(
                    (
                        "🎯 Bracket arbitrage detected "
                        f"{opportunity.event_ticker} {opportunity.arb_type} "
                        f"profit_after_fees={opportunity.profit_after_fees_cents}c "
                        f"max_sets={opportunity.max_sets}"
                    )
                )
            if arb_messages:
                decay_messages.extend(arb_messages)
            if decay_messages:
                decay_messages = self._filter_operational_alerts(now, decay_messages)
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
            "resolutions_upserted": resolution_rows_upserted,
            "prediction_accuracy_materialized": prediction_accuracy_rows_materialized,
            "arb_opportunities_detected": len(detected_arb_opportunities),
            "arb_opportunities_inserted": inserted_arb_opportunities,
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

    def run_realtime_btc_cycle(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        markets = [market for market in self._last_markets if self._is_btc_market(market)]
        if not markets:
            discovered = self.client.list_markets(self.settings.market_limit)
            self._last_markets = list(discovered)
            markets = [market for market in discovered if self._is_btc_market(market)]

        if not markets:
            return {
                "btc_markets_seen": 0,
                "btc_snapshots_inserted": 0,
                "btc_ticks_inserted": 0,
                "btc_signals_generated": 0,
                "btc_signals_inserted": 0,
                "btc_realtime_orders": 0,
                "btc_realtime_order_alert_events": 0,
            }

        ticker_to_id = self.store.upsert_markets(markets)
        snapshots_by_ticker: dict[str, MarketSnapshot] = {}
        current_snapshots: list[MarketSnapshot] = []
        orderbooks_by_ticker: dict[str, dict[str, Any]] = {}
        for market in markets:
            snapshot = None
            if self.price_provider is not None:
                try:
                    snapshot = self.price_provider.get_market_snapshot(market.ticker)
                except Exception:
                    logger.warning(
                        "realtime_snapshot_failed ticker=%s source=price_provider",
                        market.ticker,
                        exc_info=True,
                    )
            if snapshot is None:
                try:
                    snapshot = self.client.get_current_snapshot(market)
                except Exception:
                    logger.warning(
                        "realtime_snapshot_failed ticker=%s source=rest",
                        market.ticker,
                        exc_info=True,
                    )
                    continue
            snapshots_by_ticker[market.ticker] = snapshot
            current_snapshots.append(snapshot)
            orderbook = self._get_orderbook(market.ticker)
            if isinstance(orderbook, dict):
                orderbooks_by_ticker[market.ticker] = orderbook

        inserted_current = self.store.insert_snapshots(current_snapshots, ticker_to_id)

        current_ticks: list[CryptoSpotTick] = []
        if self.price_provider is not None:
            live_prices = self.price_provider.get_btc_prices()
            for source, snapshot in live_prices.items():
                current_ticks.append(
                    CryptoSpotTick(
                        ts=snapshot.timestamp,
                        source=source,
                        symbol=self.settings.btc_symbol,
                        price_usd=float(snapshot.price),
                        raw_json={"data_source": snapshot.source, "mode": "realtime"},
                    )
                )
        inserted_ticks = self.store.insert_crypto_spot_ticks(current_ticks) if current_ticks else 0

        lookback_window = max(self.settings.btc_momentum_lookback_minutes + 2, 20)
        recent_ticks = self.store.get_recent_crypto_spot_ticks(
            symbol=self.settings.btc_symbol,
            since_ts=now - timedelta(minutes=lookback_window),
        )
        btc_signals = build_btc_signals(
            self.settings,
            markets,
            snapshots_by_ticker,
            recent_ticks,
            current_ticks,
            now_utc=now,
            price_provider=self.price_provider,
            orderbooks_by_ticker=orderbooks_by_ticker,
        )
        inserted_signals = self.store.insert_signals(btc_signals) if btc_signals else 0

        order_count = 0
        alert_events_inserted = 0
        if not self.paused and self.runtime_auto_trading_enabled and btc_signals:
            orders, _paper_stats, _arb_results = self.paper_trader.execute(
                btc_signals,
                snapshots_by_ticker,
                now,
                arb_opportunities=[],
            )
            order_count = len(orders)
            if orders:
                # Realtime loop intentionally suppresses signal digests to avoid Telegram spam.
                events = self.telegram_notifier.notify(now, [], orders)
                if events:
                    alert_events_inserted = self.store.insert_alert_events(events)

        return {
            "btc_markets_seen": len(markets),
            "btc_snapshots_inserted": inserted_current,
            "btc_ticks_inserted": inserted_ticks,
            "btc_signals_generated": len(btc_signals),
            "btc_signals_inserted": inserted_signals,
            "btc_realtime_orders": order_count,
            "btc_realtime_order_alert_events": alert_events_inserted,
        }
