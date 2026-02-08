from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from .models import (
    AlertEvent,
    CryptoSpotTick,
    Market,
    MarketSnapshot,
    PaperTradeOrder,
    SignalRecord,
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
                        details,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        psycopg.types.json.Jsonb(signal.details if self.store_raw_json else signal.details),
                        signal.created_at,
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
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

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
