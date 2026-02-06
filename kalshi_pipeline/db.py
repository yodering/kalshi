from __future__ import annotations

from pathlib import Path

import psycopg

from .models import Market, MarketSnapshot


class PostgresStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.conn = psycopg.connect(database_url)

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
                        psycopg.types.json.Jsonb(market.raw_json),
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
                        psycopg.types.json.Jsonb(snapshot.raw_json),
                    ),
                )
                if cur.fetchone() is not None:
                    inserted_count += 1
        self.conn.commit()
        return inserted_count

