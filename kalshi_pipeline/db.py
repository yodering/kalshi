from __future__ import annotations

from datetime import datetime
import re
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from .models import (
    AlertEvent,
    CryptoSpotTick,
    Market,
    MarketResolution,
    MarketSnapshot,
    PaperTradeOrder,
    PredictionAccuracy,
    SignalRecord,
    WeatherBracketProbability,
    WeatherEnsembleSample,
)


class PostgresStore:
    def __init__(self, database_url: str, store_raw_json: bool = False) -> None:
        self.database_url = database_url
        self.store_raw_json = store_raw_json
        try:
            self.conn = psycopg.connect(database_url, connect_timeout=15)
        except psycopg.OperationalError as exc:
            host = urlsplit(database_url).hostname or "unknown-host"
            raise RuntimeError(
                f"Postgres connection failed for host '{host}'. "
                "Verify Railway variable wiring for DATABASE_URL."
            ) from exc

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
        schema_sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with self.conn.cursor() as cur:
            cur.execute(schema_sql)
        self.conn.commit()

    @staticmethod
    def _member_index(member: str) -> int:
        match = re.search(r"member[_-]?(\d+)", member, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
        return 0

    def upsert_markets(self, markets: list[Market]) -> dict[str, int]:
        ticker_to_id: dict[str, int] = {}
        with self.conn.cursor() as cur:
            for market in markets:
                cur.execute(
                    """
                    INSERT INTO markets (ticker, title, status, close_time, raw_json)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ticker)
                    DO UPDATE SET
                        title = EXCLUDED.title,
                        status = EXCLUDED.status,
                        close_time = EXCLUDED.close_time,
                        raw_json = EXCLUDED.raw_json,
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        market.ticker,
                        market.title,
                        market.status,
                        market.close_time,
                        psycopg.types.json.Jsonb(market.raw_json if self.store_raw_json else {}),
                    ),
                )
                ticker_to_id[market.ticker] = cur.fetchone()[0]
        self.conn.commit()
        return ticker_to_id

    def insert_snapshots(self, snapshots: list[MarketSnapshot], ticker_to_id: dict[str, int]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for snapshot in snapshots:
                market_id = ticker_to_id.get(snapshot.ticker)
                if market_id is None:
                    continue
                cur.execute(
                    """
                    INSERT INTO market_snapshots (
                        market_id,
                        snapshot_ts,
                        yes_price,
                        no_price,
                        volume,
                        raw_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (market_id, snapshot_ts)
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        market_id,
                        snapshot.ts,
                        snapshot.yes_price,
                        snapshot.no_price,
                        snapshot.volume,
                        psycopg.types.json.Jsonb(snapshot.raw_json if self.store_raw_json else {}),
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def insert_weather_ensemble_samples(self, samples: list[WeatherEnsembleSample]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for sample in samples:
                cur.execute(
                    """
                    INSERT INTO weather_ensemble_samples (
                        collected_at,
                        target_date,
                        model,
                        member,
                        max_temp_f,
                        source,
                        raw_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (collected_at, target_date, model, member)
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        sample.collected_at,
                        sample.target_date,
                        sample.model,
                        sample.member,
                        sample.max_temp_f,
                        sample.source,
                        psycopg.types.json.Jsonb(sample.raw_json if self.store_raw_json else {}),
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def insert_weather_ensemble_forecasts(self, samples: list[WeatherEnsembleSample]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for sample in samples:
                cur.execute(
                    """
                    INSERT INTO weather_ensemble_forecasts (
                        collected_at,
                        target_date,
                        model,
                        member_index,
                        predicted_max_f,
                        forecast_hour
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (collected_at, target_date, model, member_index)
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        sample.collected_at,
                        sample.target_date,
                        sample.model,
                        self._member_index(sample.member),
                        sample.max_temp_f,
                        None,
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def insert_crypto_spot_ticks(self, ticks: list[CryptoSpotTick]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for tick in ticks:
                cur.execute(
                    """
                    INSERT INTO crypto_spot_ticks (
                        ts,
                        source,
                        symbol,
                        price_usd,
                        raw_json
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (ts, source, symbol)
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        tick.ts,
                        tick.source,
                        tick.symbol,
                        tick.price_usd,
                        psycopg.types.json.Jsonb(tick.raw_json if self.store_raw_json else {}),
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def get_recent_crypto_spot_ticks(
        self, symbol: str, since_ts: datetime
    ) -> list[CryptoSpotTick]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, source, symbol, price_usd, raw_json
                FROM crypto_spot_ticks
                WHERE symbol = %s AND ts >= %s
                ORDER BY ts ASC
                """,
                (symbol, since_ts),
            )
            rows = cur.fetchall()
        ticks: list[CryptoSpotTick] = []
        for row in rows:
            ticks.append(
                CryptoSpotTick(
                    ts=row[0],
                    source=row[1],
                    symbol=row[2],
                    price_usd=float(row[3]),
                    raw_json=row[4] if isinstance(row[4], dict) else {},
                )
            )
        return ticks

    def get_latest_spot_tick(
        self, *, source: str, symbol: str
    ) -> dict[str, object] | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT ts, source, symbol, price_usd
                FROM crypto_spot_ticks
                WHERE source = %s AND symbol = %s
                ORDER BY ts DESC
                LIMIT 1
                """,
                (source, symbol),
            )
            row = cur.fetchone()
        if row is None:
            return None
        ts = row[0]
        if isinstance(ts, datetime):
            if ts.tzinfo is not None:
                age_seconds = max(0.0, (datetime.now(ts.tzinfo) - ts).total_seconds())
            else:
                age_seconds = max(0.0, (datetime.utcnow() - ts).total_seconds())
        else:
            age_seconds = 0.0
        return {
            "ts": ts,
            "source": row[1],
            "symbol": row[2],
            "price_usd": float(row[3]),
            "age_seconds": age_seconds,
        }

    def insert_signals(self, signals: list[SignalRecord]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for signal in signals:
                cur.execute(
                    """
                    INSERT INTO signals (
                        signal_type,
                        market_ticker,
                        direction,
                        model_probability,
                        market_probability,
                        edge_bps,
                        confidence,
                        data_source,
                        vwap_cents,
                        fillable_qty,
                        liquidity_sufficient,
                        details,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        signal.signal_type,
                        signal.market_ticker,
                        signal.direction,
                        signal.model_probability,
                        signal.market_probability,
                        signal.edge_bps,
                        signal.confidence,
                        signal.data_source,
                        signal.vwap_cents,
                        signal.fillable_qty,
                        signal.liquidity_sufficient,
                        psycopg.types.json.Jsonb(signal.details if self.store_raw_json else signal.details),
                        signal.created_at,
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def insert_weather_bracket_probabilities(
        self, rows: list[WeatherBracketProbability]
    ) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO weather_bracket_probs (
                        computed_at,
                        target_date,
                        ticker,
                        bracket_low,
                        bracket_high,
                        model_prob,
                        market_prob,
                        edge,
                        ensemble_count
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (computed_at, ticker)
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        row.computed_at,
                        row.target_date,
                        row.ticker,
                        row.bracket_low,
                        row.bracket_high,
                        row.model_prob,
                        row.market_prob,
                        row.edge,
                        row.ensemble_count,
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def upsert_market_resolutions(self, rows: list[MarketResolution]) -> int:
        updated_count = 0
        with self.conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO market_resolutions (
                        ticker,
                        series_ticker,
                        event_ticker,
                        market_type,
                        resolved_at,
                        result,
                        actual_value,
                        resolution_source,
                        collected_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (ticker)
                    DO UPDATE SET
                        series_ticker = EXCLUDED.series_ticker,
                        event_ticker = EXCLUDED.event_ticker,
                        market_type = EXCLUDED.market_type,
                        resolved_at = EXCLUDED.resolved_at,
                        result = EXCLUDED.result,
                        actual_value = EXCLUDED.actual_value,
                        resolution_source = EXCLUDED.resolution_source,
                        collected_at = EXCLUDED.collected_at
                    RETURNING ticker
                    """,
                    (
                        row.ticker,
                        row.series_ticker,
                        row.event_ticker,
                        row.market_type,
                        row.resolved_at,
                        row.result,
                        row.actual_value,
                        row.resolution_source,
                        row.collected_at,
                    ),
                )
                if cur.fetchone() is not None:
                    updated_count += 1
        self.conn.commit()
        return updated_count

    def insert_prediction_accuracy(self, rows: list[PredictionAccuracy]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO prediction_accuracy (
                        signal_id,
                        ticker,
                        signal_time,
                        model_prob,
                        market_prob,
                        edge_bps,
                        actual_outcome,
                        pnl_per_contract,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        row.signal_id,
                        row.ticker,
                        row.signal_time,
                        row.model_prob,
                        row.market_prob,
                        row.edge_bps,
                        row.actual_outcome,
                        row.pnl_per_contract,
                        row.created_at,
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def has_recent_paper_order(
        self, market_ticker: str, direction: str, since_ts: datetime
    ) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM paper_trade_orders
                WHERE market_ticker = %s
                  AND direction = %s
                  AND created_at >= %s
                LIMIT 1
                """,
                (market_ticker, direction, since_ts),
            )
            row = cur.fetchone()
        return row is not None

    def insert_paper_trade_orders(self, orders: list[PaperTradeOrder]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for order in orders:
                cur.execute(
                    """
                    INSERT INTO paper_trade_orders (
                        market_ticker,
                        signal_type,
                        direction,
                        side,
                        count,
                        limit_price_cents,
                        provider,
                        status,
                        reason,
                        external_order_id,
                        request_payload,
                        response_payload,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        order.market_ticker,
                        order.signal_type,
                        order.direction,
                        order.side,
                        order.count,
                        order.limit_price_cents,
                        order.provider,
                        order.status,
                        order.reason,
                        order.external_order_id,
                        psycopg.types.json.Jsonb(
                            order.request_payload if self.store_raw_json else order.request_payload
                        ),
                        psycopg.types.json.Jsonb(
                            order.response_payload
                            if self.store_raw_json
                            else order.response_payload
                        ),
                        order.created_at,
                    ),
                )
                inserted_row = cur.fetchone()
                if inserted_row is not None:
                    inserted_count += 1
                    order_id = inserted_row[0]
                    cur.execute(
                        """
                        INSERT INTO paper_trade_order_events (
                            order_id,
                            market_ticker,
                            external_order_id,
                            status,
                            details,
                            event_ts
                        )
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            order_id,
                            order.market_ticker,
                            order.external_order_id,
                            order.status,
                            psycopg.types.json.Jsonb({"provider": order.provider}),
                            order.created_at,
                        ),
                    )
        self.conn.commit()
        return inserted_count

    def insert_order_event(
        self,
        *,
        market_ticker: str,
        status: str,
        event_ts: datetime,
        order_id: int | None = None,
        external_order_id: str | None = None,
        queue_position: int | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO paper_trade_order_events (
                    order_id,
                    market_ticker,
                    external_order_id,
                    status,
                    queue_position,
                    details,
                    event_ts
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    market_ticker,
                    external_order_id,
                    status,
                    queue_position,
                    psycopg.types.json.Jsonb(details or {}),
                    event_ts,
                ),
            )
        self.conn.commit()

    def insert_alert_events(self, events: list[AlertEvent]) -> int:
        inserted_count = 0
        with self.conn.cursor() as cur:
            for event in events:
                cur.execute(
                    """
                    INSERT INTO alert_events (
                        channel,
                        event_type,
                        market_ticker,
                        message,
                        status,
                        metadata,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        event.channel,
                        event.event_type,
                        event.market_ticker,
                        event.message,
                        event.status,
                        psycopg.types.json.Jsonb(
                            event.metadata if self.store_raw_json else event.metadata
                        ),
                        event.created_at,
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

    def insert_bracket_arb_opportunities(self, rows: list[dict[str, object]]) -> list[int]:
        inserted_ids: list[int] = []
        with self.conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO bracket_arb_opportunities (
                        detected_at,
                        event_ticker,
                        arb_type,
                        n_brackets,
                        cost_cents,
                        payout_cents,
                        profit_cents,
                        profit_after_fees_cents,
                        max_sets,
                        total_profit_cents,
                        legs,
                        executed,
                        execution_result
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        row.get("detected_at"),
                        row.get("event_ticker"),
                        row.get("arb_type"),
                        int(row.get("n_brackets") or 0),
                        int(row.get("cost_cents") or 0),
                        int(row.get("payout_cents") or 0),
                        int(row.get("profit_cents") or 0),
                        int(row.get("profit_after_fees_cents") or 0),
                        int(row.get("max_sets") or 0),
                        int(row.get("total_profit_cents") or 0),
                        psycopg.types.json.Jsonb(row.get("legs") or []),
                        bool(row.get("executed", False)),
                        psycopg.types.json.Jsonb(row.get("execution_result") or {}),
                    ),
                )
                inserted_row = cur.fetchone()
                if inserted_row is not None:
                    inserted_ids.append(int(inserted_row[0]))
        self.conn.commit()
        return inserted_ids

    def get_recent_bracket_arb_opportunities(
        self, *, days: int = 7, limit: int = 50
    ) -> list[dict[str, object]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    detected_at,
                    event_ticker,
                    arb_type,
                    n_brackets,
                    cost_cents,
                    payout_cents,
                    profit_cents,
                    profit_after_fees_cents,
                    max_sets,
                    total_profit_cents,
                    legs,
                    executed,
                    execution_result
                FROM bracket_arb_opportunities
                WHERE detected_at >= NOW() - (%s || ' days')::interval
                ORDER BY detected_at DESC
                LIMIT %s
                """,
                (max(1, days), max(1, limit)),
            )
            rows = cur.fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            output.append(
                {
                    "id": int(row[0]),
                    "detected_at": row[1],
                    "event_ticker": row[2],
                    "arb_type": row[3],
                    "n_brackets": int(row[4] or 0),
                    "cost_cents": int(row[5] or 0),
                    "payout_cents": int(row[6] or 0),
                    "profit_cents": int(row[7] or 0),
                    "profit_after_fees_cents": int(row[8] or 0),
                    "max_sets": int(row[9] or 0),
                    "total_profit_cents": int(row[10] or 0),
                    "legs": row[11] if isinstance(row[11], list) else [],
                    "executed": bool(row[12]),
                    "execution_result": row[13] if isinstance(row[13], dict) else {},
                }
            )
        return output

    def mark_bracket_arb_executed(
        self, *, opportunity_id: int, execution_result: dict[str, object]
    ) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bracket_arb_opportunities
                SET executed = TRUE,
                    execution_result = %s
                WHERE id = %s
                RETURNING id
                """,
                (psycopg.types.json.Jsonb(execution_result), opportunity_id),
            )
            updated = cur.fetchone() is not None
        self.conn.commit()
        return updated

    def get_recent_signals(
        self, *, limit: int = 5, signal_type: str | None = None
    ) -> list[dict[str, object]]:
        query = """
            SELECT id, created_at, signal_type, market_ticker, direction, model_probability,
                   market_probability, edge_bps, confidence, data_source,
                   vwap_cents, fillable_qty, liquidity_sufficient, details
            FROM signals
        """
        params: list[object] = []
        if signal_type:
            query += " WHERE signal_type = %s"
            params.append(signal_type)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            output.append(
                {
                    "id": row[0],
                    "created_at": row[1],
                    "signal_type": row[2],
                    "market_ticker": row[3],
                    "direction": row[4],
                    "model_probability": row[5],
                    "market_probability": row[6],
                    "edge_bps": row[7],
                    "confidence": row[8],
                    "data_source": row[9],
                    "vwap_cents": row[10],
                    "fillable_qty": row[11],
                    "liquidity_sufficient": row[12],
                    "details": row[13] if isinstance(row[13], dict) else {},
                }
            )
        return output

    def get_recent_paper_orders(self, *, limit: int = 20) -> list[dict[str, object]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, market_ticker, direction, side, count,
                       limit_price_cents, status, reason, external_order_id
                FROM paper_trade_orders
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "created_at": row[1],
                "market_ticker": row[2],
                "direction": row[3],
                "side": row[4],
                "count": row[5],
                "limit_price_cents": row[6],
                "status": row[7],
                "reason": row[8],
                "external_order_id": row[9],
            }
            for row in rows
        ]

    def get_order_status_counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*)
                FROM paper_trade_orders
                GROUP BY status
                """
            )
            rows = cur.fetchall()
        return {str(status): int(count) for status, count in rows}

    def get_paper_fill_metrics(self, *, days: int = 30) -> dict[str, float | int | None]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                WITH scoped_orders AS (
                    SELECT id, status
                    FROM paper_trade_orders
                    WHERE provider = 'kalshi_demo'
                      AND created_at >= NOW() - (%s || ' days')::interval
                      AND status <> 'simulated'
                ),
                first_open AS (
                    SELECT order_id, MIN(event_ts) AS first_open_ts
                    FROM paper_trade_order_events
                    WHERE status IN ('submitted', 'resting', 'partially_filled')
                    GROUP BY order_id
                ),
                first_fill AS (
                    SELECT order_id, MIN(event_ts) AS first_fill_ts
                    FROM paper_trade_order_events
                    WHERE status = 'filled'
                    GROUP BY order_id
                )
                SELECT
                    COUNT(*)::INT AS total_orders,
                    COUNT(*) FILTER (WHERE so.status = 'filled')::INT AS filled_orders,
                    COUNT(*) FILTER (WHERE so.status IN ('submitted', 'partially_filled'))::INT AS open_orders,
                    COUNT(*) FILTER (WHERE so.status = 'canceled')::INT AS canceled_orders,
                    COUNT(*) FILTER (WHERE so.status = 'failed')::INT AS failed_orders,
                    AVG(EXTRACT(EPOCH FROM (ff.first_fill_ts - fo.first_open_ts))) AS avg_fill_seconds
                FROM scoped_orders so
                LEFT JOIN first_open fo ON fo.order_id = so.id
                LEFT JOIN first_fill ff ON ff.order_id = so.id
                """,
                (days,),
            )
            row = cur.fetchone()

        if row is None:
            return {
                "days": days,
                "total_orders": 0,
                "filled_orders": 0,
                "open_orders": 0,
                "canceled_orders": 0,
                "failed_orders": 0,
                "fill_rate": None,
                "avg_fill_minutes": None,
            }

        total_orders = int(row[0] or 0)
        filled_orders = int(row[1] or 0)
        open_orders = int(row[2] or 0)
        canceled_orders = int(row[3] or 0)
        failed_orders = int(row[4] or 0)
        settled_orders = filled_orders + canceled_orders + failed_orders
        fill_rate = (filled_orders / settled_orders) if settled_orders > 0 else None
        avg_fill_seconds = float(row[5]) if row[5] is not None else None
        avg_fill_minutes = (avg_fill_seconds / 60.0) if avg_fill_seconds is not None else None

        return {
            "days": days,
            "total_orders": total_orders,
            "filled_orders": filled_orders,
            "open_orders": open_orders,
            "canceled_orders": canceled_orders,
            "failed_orders": failed_orders,
            "fill_rate": fill_rate,
            "avg_fill_minutes": avg_fill_minutes,
        }

    def estimate_fill_probability(
        self,
        *,
        ticker_prefix: str,
        lookback_days: int = 14,
        min_price_cents: int | None = None,
        max_price_cents: int | None = None,
        min_samples: int = 20,
    ) -> float | None:
        cleaned_prefix = ticker_prefix.strip().upper()
        if not cleaned_prefix:
            return None

        where_sql = [
            "provider = 'kalshi_demo'",
            "status <> 'simulated'",
            "signal_type <> 'arbitrage'",
            "market_ticker LIKE %s",
            "created_at >= NOW() - (%s || ' days')::interval",
        ]
        params: list[object] = [f"{cleaned_prefix}%", max(1, int(lookback_days))]
        if min_price_cents is not None:
            where_sql.append("limit_price_cents >= %s")
            params.append(int(min_price_cents))
        if max_price_cents is not None:
            where_sql.append("limit_price_cents <= %s")
            params.append(int(max_price_cents))

        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE status IN ('filled', 'partially_filled')
                    )::INT AS filled_like_orders,
                    COUNT(*) FILTER (
                        WHERE status IN ('filled', 'partially_filled', 'canceled', 'failed')
                    )::INT AS settled_orders
                FROM paper_trade_orders
                WHERE {" AND ".join(where_sql)}
                """,
                tuple(params),
            )
            row = cur.fetchone()

        if row is None:
            return None
        filled_like_orders = int(row[0] or 0)
        settled_orders = int(row[1] or 0)
        if settled_orders < max(1, int(min_samples)):
            return None
        return filled_like_orders / float(settled_orders)

    def get_recent_alert_events(self, *, limit: int = 10) -> list[dict[str, object]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT created_at, event_type, status, market_ticker
                FROM alert_events
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "created_at": row[0],
                "event_type": row[1],
                "status": row[2],
                "market_ticker": row[3],
            }
            for row in rows
        ]

    def get_open_positions_summary(self) -> list[dict[str, object]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT market_ticker, side, SUM(count) AS contracts,
                       AVG(limit_price_cents)::DOUBLE PRECISION AS avg_price_cents
                FROM paper_trade_orders
                WHERE status IN ('submitted', 'partially_filled')
                GROUP BY market_ticker, side
                ORDER BY market_ticker
                """
            )
            rows = cur.fetchall()
        return [
            {
                "market_ticker": row[0],
                "side": row[1],
                "contracts": int(row[2] or 0),
                "avg_price_cents": float(row[3] or 0.0),
            }
            for row in rows
        ]

    def get_submitted_paper_orders(
        self,
        *,
        limit: int = 200,
        since_ts: datetime | None = None,
    ) -> list[dict[str, object]]:
        query = """
            SELECT id, created_at, market_ticker, signal_type, direction, side,
                   count, limit_price_cents, status, reason, external_order_id
            FROM paper_trade_orders
            WHERE status IN ('submitted', 'partially_filled')
              AND external_order_id IS NOT NULL
        """
        params: list[object] = []
        if since_ts is not None:
            query += " AND created_at >= %s"
            params.append(since_ts)
        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self.conn.cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
        return [
            {
                "id": row[0],
                "created_at": row[1],
                "market_ticker": row[2],
                "signal_type": row[3],
                "direction": row[4],
                "side": row[5],
                "count": row[6],
                "limit_price_cents": row[7],
                "status": row[8],
                "reason": row[9],
                "external_order_id": row[10],
            }
            for row in rows
        ]

    def get_latest_order_events(
        self, order_ids: list[int]
    ) -> dict[int, dict[str, object]]:
        cleaned_ids = [int(order_id) for order_id in order_ids if order_id is not None]
        if not cleaned_ids:
            return {}
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (order_id)
                       order_id, status, queue_position, event_ts, details
                FROM paper_trade_order_events
                WHERE order_id = ANY(%s)
                ORDER BY order_id, event_ts DESC, id DESC
                """,
                (cleaned_ids,),
            )
            rows = cur.fetchall()
        output: dict[int, dict[str, object]] = {}
        for row in rows:
            output[int(row[0])] = {
                "status": row[1],
                "queue_position": row[2],
                "event_ts": row[3],
                "details": row[4] if isinstance(row[4], dict) else {},
            }
        return output

    def get_recent_reprice_timestamps(
        self,
        *,
        market_ticker: str,
        since_ts: datetime,
        limit: int = 50,
    ) -> list[datetime]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_ts
                FROM paper_trade_order_events
                WHERE market_ticker = %s
                  AND status = 'reprice_submitted'
                  AND event_ts >= %s
                ORDER BY event_ts DESC
                LIMIT %s
                """,
                (market_ticker, since_ts, max(1, int(limit))),
            )
            rows = cur.fetchall()
        return [row[0] for row in rows if isinstance(row[0], datetime)]

    def update_paper_trade_order_status(
        self,
        *,
        order_id: int,
        status: str,
        reason: str | None = None,
        response_payload: dict[str, object] | None = None,
    ) -> bool:
        payload = response_payload or {}
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE paper_trade_orders
                SET status = %s,
                    reason = COALESCE(%s, reason),
                    response_payload = CASE
                        WHEN %s = '{}'::jsonb THEN response_payload
                        ELSE %s
                    END
                WHERE id = %s
                RETURNING id
                """,
                (
                    status,
                    reason,
                    psycopg.types.json.Jsonb(payload),
                    psycopg.types.json.Jsonb(payload),
                    order_id,
                ),
            )
            updated = cur.fetchone() is not None
        self.conn.commit()
        return updated

    def materialize_prediction_accuracy(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prediction_accuracy (
                    signal_id,
                    ticker,
                    signal_time,
                    model_prob,
                    market_prob,
                    edge_bps,
                    actual_outcome,
                    pnl_per_contract,
                    created_at
                )
                SELECT
                    s.id,
                    s.market_ticker,
                    s.created_at,
                    s.model_probability,
                    s.market_probability,
                    s.edge_bps,
                    CASE
                        WHEN lower(r.result) = 'yes' THEN TRUE
                        WHEN lower(r.result) = 'no' THEN FALSE
                        ELSE NULL
                    END AS actual_outcome,
                    CASE
                        WHEN lower(r.result) NOT IN ('yes', 'no') OR s.market_probability IS NULL THEN NULL
                        WHEN s.direction = 'buy_yes' AND lower(r.result) = 'yes'
                            THEN 100 - (s.market_probability * 100.0)
                        WHEN s.direction = 'buy_yes' AND lower(r.result) = 'no'
                            THEN -1.0 * (s.market_probability * 100.0)
                        WHEN s.direction = 'buy_no' AND lower(r.result) = 'no'
                            THEN s.market_probability * 100.0
                        WHEN s.direction = 'buy_no' AND lower(r.result) = 'yes'
                            THEN -1.0 * (100 - (s.market_probability * 100.0))
                        ELSE NULL
                    END AS pnl_per_contract,
                    NOW()
                FROM signals s
                JOIN market_resolutions r
                  ON r.ticker = s.market_ticker
                LEFT JOIN prediction_accuracy p
                  ON p.signal_id = s.id
                WHERE s.market_ticker IS NOT NULL
                  AND s.direction IN ('buy_yes', 'buy_no')
                  AND r.resolved_at IS NOT NULL
                  AND s.created_at <= r.resolved_at
                  AND p.id IS NULL
                """
            )
            inserted = cur.rowcount or 0
        self.conn.commit()
        return int(inserted)

    def get_accuracy_metrics(self, *, days: int = 30, signal_type: str | None = None) -> dict[str, object]:
        where_parts = ["pa.signal_time >= NOW() - (%s || ' days')::interval"]
        params: list[object] = [days]
        if signal_type:
            where_parts.append("s.signal_type = %s")
            params.append(signal_type)
        where_sql = " AND ".join(where_parts)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    COUNT(*) AS n_signals,
                    AVG(
                        CASE
                            WHEN pa.model_prob IS NULL OR pa.actual_outcome IS NULL THEN NULL
                            ELSE POWER(pa.model_prob - CASE WHEN pa.actual_outcome THEN 1.0 ELSE 0.0 END, 2)
                        END
                    ) AS brier_score,
                    AVG(
                        CASE
                            WHEN pa.market_prob IS NULL OR pa.actual_outcome IS NULL THEN NULL
                            ELSE POWER(pa.market_prob - CASE WHEN pa.actual_outcome THEN 1.0 ELSE 0.0 END, 2)
                        END
                    ) AS market_brier_score,
                    AVG(
                        CASE
                            WHEN pa.model_prob IS NULL OR pa.actual_outcome IS NULL THEN NULL
                            WHEN pa.actual_outcome THEN -LN(LEAST(0.999999, GREATEST(0.000001, pa.model_prob)))
                            ELSE -LN(LEAST(0.999999, GREATEST(0.000001, 1.0 - pa.model_prob)))
                        END
                    ) AS log_loss,
                    AVG(
                        CASE
                            WHEN pa.edge_bps IS NULL OR pa.pnl_per_contract IS NULL OR pa.edge_bps <= 0 THEN NULL
                            WHEN pa.pnl_per_contract > 0 THEN 1.0
                            ELSE 0.0
                        END
                    ) AS edge_reliability,
                    AVG(
                        CASE
                            WHEN pa.actual_outcome IS NULL THEN NULL
                            WHEN s.direction = 'buy_yes' AND pa.actual_outcome THEN 1.0
                            WHEN s.direction = 'buy_no' AND NOT pa.actual_outcome THEN 1.0
                            ELSE 0.0
                        END
                    ) AS hit_rate,
                    AVG(pa.pnl_per_contract) AS avg_pnl_per_contract,
                    SUM(pa.pnl_per_contract) AS total_pnl
                FROM prediction_accuracy pa
                LEFT JOIN signals s ON s.id = pa.signal_id
                WHERE {where_sql}
                """,
                tuple(params),
            )
            row = cur.fetchone()
        if row is None:
            return {
                "days": days,
                "n_signals": 0,
                "brier_score": None,
                "market_brier_score": None,
                "log_loss": None,
                "edge_reliability": None,
                "hit_rate": None,
                "avg_pnl_per_contract": None,
                "total_pnl": None,
            }
        return {
            "days": days,
            "n_signals": int(row[0] or 0),
            "brier_score": float(row[1]) if row[1] is not None else None,
            "market_brier_score": float(row[2]) if row[2] is not None else None,
            "log_loss": float(row[3]) if row[3] is not None else None,
            "edge_reliability": float(row[4]) if row[4] is not None else None,
            "hit_rate": float(row[5]) if row[5] is not None else None,
            "avg_pnl_per_contract": float(row[6]) if row[6] is not None else None,
            "total_pnl": float(row[7]) if row[7] is not None else None,
        }

    def get_calibration_curve(
        self, *, days: int = 30, signal_type: str | None = None
    ) -> list[dict[str, object]]:
        where_parts = ["pa.signal_time >= NOW() - (%s || ' days')::interval"]
        params: list[object] = [days]
        if signal_type:
            where_parts.append("s.signal_type = %s")
            params.append(signal_type)
        where_sql = " AND ".join(where_parts)
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    width_bucket(pa.model_prob, 0.0, 1.0, 10) AS bucket,
                    AVG(pa.model_prob) AS avg_predicted,
                    AVG(CASE WHEN pa.actual_outcome THEN 1.0 ELSE 0.0 END) AS actual_rate,
                    COUNT(*) AS n
                FROM prediction_accuracy pa
                LEFT JOIN signals s ON s.id = pa.signal_id
                WHERE pa.model_prob IS NOT NULL
                  AND pa.actual_outcome IS NOT NULL
                  AND {where_sql}
                GROUP BY bucket
                ORDER BY bucket
                """,
                tuple(params),
            )
            rows = cur.fetchall()
        return [
            {
                "bucket": int(row[0]),
                "avg_predicted": float(row[1]) if row[1] is not None else None,
                "actual_rate": float(row[2]) if row[2] is not None else None,
                "count": int(row[3] or 0),
            }
            for row in rows
        ]

    def get_recent_market_snapshots(
        self, *, ticker: str, limit: int = 20
    ) -> list[dict[str, object]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT ms.snapshot_ts, ms.yes_price, ms.no_price, ms.volume
                FROM market_snapshots ms
                JOIN markets m ON m.id = ms.market_id
                WHERE m.ticker = %s
                ORDER BY ms.snapshot_ts DESC
                LIMIT %s
                """,
                (ticker, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "snapshot_ts": row[0],
                "yes_price": row[1],
                "no_price": row[2],
                "volume": row[3],
            }
            for row in rows
        ]

    def get_recent_weather_ensemble_samples(
        self, *, limit: int = 20
    ) -> list[dict[str, object]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT collected_at, target_date, model, member, max_temp_f
                FROM weather_ensemble_samples
                ORDER BY collected_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "collected_at": row[0],
                "target_date": row[1],
                "model": row[2],
                "member": row[3],
                "max_temp_f": row[4],
            }
            for row in rows
        ]

    def get_weather_backtest_rows(self, *, days: int = 30) -> list[dict[str, object]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                WITH latest_probs AS (
                    SELECT DISTINCT ON (w.target_date, w.ticker)
                        w.target_date,
                        w.ticker,
                        w.bracket_low,
                        w.bracket_high,
                        w.model_prob,
                        w.market_prob,
                        w.edge,
                        w.ensemble_count,
                        w.computed_at,
                        r.result,
                        r.actual_value,
                        r.resolved_at
                    FROM weather_bracket_probs w
                    JOIN market_resolutions r ON r.ticker = w.ticker
                    WHERE r.resolved_at IS NOT NULL
                      AND w.computed_at <= r.resolved_at
                      AND w.computed_at >= NOW() - (%s || ' days')::interval
                      AND lower(r.result) IN ('yes', 'no')
                    ORDER BY w.target_date, w.ticker, w.computed_at DESC
                )
                SELECT
                    target_date,
                    ticker,
                    bracket_low,
                    bracket_high,
                    model_prob,
                    market_prob,
                    edge,
                    ensemble_count,
                    computed_at,
                    resolved_at,
                    result,
                    CASE WHEN lower(result) = 'yes' THEN 1 ELSE 0 END AS actual_outcome,
                    actual_value
                FROM latest_probs
                ORDER BY target_date DESC, ticker ASC
                """,
                (max(1, days),),
            )
            rows = cur.fetchall()
        output: list[dict[str, object]] = []
        for row in rows:
            output.append(
                {
                    "target_date": row[0],
                    "ticker": row[1],
                    "bracket_low": row[2],
                    "bracket_high": row[3],
                    "model_prob": float(row[4]) if row[4] is not None else None,
                    "market_prob": float(row[5]) if row[5] is not None else None,
                    "edge": float(row[6]) if row[6] is not None else None,
                    "ensemble_count": int(row[7] or 0),
                    "computed_at": row[8],
                    "resolved_at": row[9],
                    "result": row[10],
                    "actual_outcome": int(row[11] or 0),
                    "actual_value": float(row[12]) if row[12] is not None else None,
                }
            )
        return output
