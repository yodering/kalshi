from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import Settings, redact_database_url
from .kalshi_client import KalshiClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi Week 1 data pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health-check", help="Run Kalshi API connectivity check")
    subparsers.add_parser("init-db", help="Create database schema")
    subparsers.add_parser("discover-targets", help="List markets matched by TARGET_* filters")
    subparsers.add_parser("run-once", help="Run one polling cycle")
    subparsers.add_parser("run", help="Run continuous polling loop")
    return parser


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    logger = logging.getLogger(__name__)
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    logger.info(
        "startup kalshi_stub_mode=%s kalshi_auth_for_public=%s database_source=%s database_target=%s target_groups=%s target_tickers=%s target_series=%s auto_select_live_contracts=%s store_raw_json=%s weather_enabled=%s btc_enabled=%s signal_min_edge_bps=%s",
        settings.kalshi_stub_mode,
        settings.kalshi_use_auth_for_public_data,
        settings.database_url_source,
        redact_database_url(settings.database_url),
        ";".join(settings.target_market_query_groups),
        ",".join(settings.target_market_tickers),
        ",".join(settings.target_series_tickers),
        settings.auto_select_live_contracts,
        settings.store_raw_json,
        settings.weather_enabled,
        settings.btc_enabled,
        settings.signal_min_edge_bps,
    )

    if args.command == "health-check":
        client = KalshiClient(settings)
        print(json.dumps(client.health_check(), indent=2))
        return 0

    if args.command == "discover-targets":
        client = KalshiClient(settings)
        markets = client.list_markets(settings.market_limit)
        preview = [
            {"ticker": market.ticker, "title": market.title, "status": market.status}
            for market in markets
        ]
        print(json.dumps(preview, indent=2))
        return 0

    from .db import PostgresStore
    from .pipeline import DataPipeline

    store = PostgresStore(settings.database_url, store_raw_json=settings.store_raw_json)
    try:
        if args.command == "init-db":
            store.ensure_schema()
            print("Schema initialized")
            return 0

        store.ensure_schema()
        client = KalshiClient(settings)
        pipeline = DataPipeline(settings, client, store)

        if args.command == "run-once":
            print(json.dumps(pipeline.run_once(), indent=2))
            return 0

        if args.command == "run":
            pipeline.run_forever()
            return 0
    finally:
        store.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())
