from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
import logging
import sys

from .analysis.accuracy_report import generate_accuracy_report
from .config import Settings
from .db import PostgresStore
from .kalshi_client import KalshiClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kalshi bot operations CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show pipeline status snapshot")
    subparsers.add_parser("positions", help="Show open submitted paper positions")
    subparsers.add_parser("balance", help="Fetch portfolio balance in demo mode")

    signals_cmd = subparsers.add_parser("signals", help="Show recent signals")
    signals_cmd.add_argument("--last", type=int, default=20)

    trades_cmd = subparsers.add_parser("trades", help="Show recent paper trades")
    trades_cmd.add_argument("--last", type=int, default=20)

    accuracy_cmd = subparsers.add_parser("accuracy", help="Show accuracy report")
    accuracy_cmd.add_argument("--days", type=int, default=30)
    accuracy_cmd.add_argument("--market-type", default="all")

    orderbook_cmd = subparsers.add_parser("orderbook", help="Fetch live orderbook-style market view")
    orderbook_cmd.add_argument("ticker")

    forecast_cmd = subparsers.add_parser("forecast", help="Show recent weather samples")
    forecast_cmd.add_argument("--last", type=int, default=40)

    subparsers.add_parser("ws-status", help="Show websocket readiness snapshot")
    return parser


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _run_status(store: PostgresStore) -> None:
    _print_json(
        {
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "order_status_counts": store.get_order_status_counts(),
            "recent_alerts": store.get_recent_alert_events(limit=5),
            "recent_orders": store.get_recent_paper_orders(limit=5),
        }
    )


def _run_positions(store: PostgresStore) -> None:
    _print_json(store.get_open_positions_summary())


def _run_balance(settings: Settings, client: KalshiClient) -> None:
    if settings.paper_trading_mode != "kalshi_demo":
        _print_json({"error": "balance unavailable unless PAPER_TRADING_MODE=kalshi_demo"})
        return
    payload = client._request_json(  # noqa: SLF001
        "GET",
        "/trade-api/v2/portfolio/balance",
        require_auth=True,
        base_url_override=settings.paper_trading_base_url,
    )
    _print_json(payload)


def _run_signals(store: PostgresStore, last: int) -> None:
    _print_json(store.get_recent_signals(limit=max(1, last)))


def _run_trades(store: PostgresStore, last: int) -> None:
    _print_json(store.get_recent_paper_orders(limit=max(1, last)))


def _run_accuracy(store: PostgresStore, market_type: str, days: int) -> None:
    report = generate_accuracy_report(store, market_type=market_type, days=max(1, days))
    _print_json(report.to_dict())


def _run_orderbook(client: KalshiClient, ticker: str) -> None:
    payload = client._request_json("GET", f"/trade-api/v2/markets/{ticker}")  # noqa: SLF001
    _print_json(payload)


def _run_forecast(store: PostgresStore, last: int) -> None:
    _print_json(store.get_recent_weather_ensemble_samples(limit=max(1, last)))


def _run_ws_status(store: PostgresStore) -> None:
    recent_signals = store.get_recent_signals(limit=3)
    recent_orders = store.get_recent_paper_orders(limit=3)
    _print_json(
        {
            "ws_ready": True,
            "note": "Websocket modules are available; runtime activation depends on command mode.",
            "recent_signals": recent_signals,
            "recent_orders": recent_orders,
        }
    )


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    store = PostgresStore(settings.database_url, store_raw_json=settings.store_raw_json)
    client = KalshiClient(settings)
    try:
        if args.command == "status":
            _run_status(store)
            return 0
        if args.command == "positions":
            _run_positions(store)
            return 0
        if args.command == "balance":
            _run_balance(settings, client)
            return 0
        if args.command == "signals":
            _run_signals(store, args.last)
            return 0
        if args.command == "trades":
            _run_trades(store, args.last)
            return 0
        if args.command == "accuracy":
            _run_accuracy(store, args.market_type, args.days)
            return 0
        if args.command == "orderbook":
            _run_orderbook(client, args.ticker)
            return 0
        if args.command == "forecast":
            _run_forecast(store, args.last)
            return 0
        if args.command == "ws-status":
            _run_ws_status(store)
            return 0
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
