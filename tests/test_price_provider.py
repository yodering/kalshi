from __future__ import annotations

from datetime import datetime, timezone
import sys
import types
import unittest

# price_provider imports db -> psycopg at import time; stub it for unit tests.
if "psycopg" not in sys.modules:
    psycopg_stub = types.ModuleType("psycopg")
    psycopg_stub.OperationalError = Exception
    psycopg_stub.connect = lambda *args, **kwargs: None
    psycopg_stub.types = types.SimpleNamespace(
        json=types.SimpleNamespace(Jsonb=lambda value: value)
    )
    sys.modules["psycopg"] = psycopg_stub

from kalshi_pipeline.data.price_provider import PriceProvider


class _FakeFeed:
    def __init__(
        self,
        *,
        is_connected: bool,
        age_seconds: float,
        price: float | None,
        ts: datetime | None = None,
    ) -> None:
        self.is_connected = is_connected
        self.age_seconds = age_seconds
        self._price = price
        self.last_update_time = ts or datetime.now(timezone.utc)

    def get_latest_price(self) -> float | None:
        return self._price


class _FakeKalshiFeed:
    def __init__(self, orderbooks: dict[str, dict[str, object]]) -> None:
        self.is_connected = True
        self._orderbooks = orderbooks

    def has_orderbook(self, ticker: str) -> bool:
        return ticker in self._orderbooks

    def get_orderbook_age_seconds(self, ticker: str) -> float | None:
        if ticker not in self._orderbooks:
            return None
        return 1.0

    def get_orderbook(self, ticker: str) -> dict[str, object]:
        return self._orderbooks[ticker]


class _FakeStore:
    def __init__(self, fallback_rows: dict[str, dict[str, object]]) -> None:
        self._fallback_rows = fallback_rows

    def get_latest_spot_tick(self, *, source: str, symbol: str) -> dict[str, object] | None:
        _ = symbol
        return self._fallback_rows.get(source)

    def get_recent_crypto_spot_ticks(self, symbol: str, since_ts: datetime) -> list[object]:
        _ = symbol
        _ = since_ts
        return []


class _FakeClient:
    def __init__(self) -> None:
        self.orderbook_calls: list[str] = []

    def get_orderbook(self, ticker: str) -> dict[str, object]:
        self.orderbook_calls.append(ticker)
        return {"yes": [(40, 10)], "no": [(60, 10)], "source": "rest"}

    def _request_json(self, method: str, path: str) -> dict[str, object]:
        _ = method
        _ = path
        return {"market": {"yes_ask": 40, "no_ask": 60}}


class PriceProviderTests(unittest.TestCase):
    def test_ws_prices_preferred_when_fresh(self) -> None:
        store = _FakeStore(
            {
                "coinbase": {
                    "ts": datetime.now(timezone.utc),
                    "source": "coinbase",
                    "symbol": "BTCUSD",
                    "price_usd": 1.0,
                    "age_seconds": 1.0,
                }
            }
        )
        provider = PriceProvider(
            binance_feed=_FakeFeed(is_connected=True, age_seconds=1.0, price=50000.0),
            coinbase_feed=_FakeFeed(is_connected=True, age_seconds=2.0, price=50010.0),
            kraken_feed=_FakeFeed(is_connected=True, age_seconds=3.0, price=50020.0),
            kalshi_feed=None,
            store=store,  # type: ignore[arg-type]
            client=_FakeClient(),  # type: ignore[arg-type]
            btc_symbol="BTCUSD",
        )
        prices = provider.get_btc_prices()
        self.assertEqual(prices["binance"].source, "ws")
        self.assertEqual(prices["coinbase"].source, "ws")
        self.assertEqual(prices["kraken"].source, "ws")

    def test_stale_ws_falls_back_to_db_tick(self) -> None:
        now = datetime.now(timezone.utc)
        store = _FakeStore(
            {
                "coinbase": {
                    "ts": now,
                    "source": "coinbase",
                    "symbol": "BTCUSD",
                    "price_usd": 47000.0,
                    "age_seconds": 3.0,
                }
            }
        )
        provider = PriceProvider(
            binance_feed=None,
            coinbase_feed=_FakeFeed(is_connected=True, age_seconds=9.0, price=51000.0, ts=now),
            kraken_feed=None,
            kalshi_feed=None,
            store=store,  # type: ignore[arg-type]
            client=_FakeClient(),  # type: ignore[arg-type]
            btc_symbol="BTCUSD",
        )
        prices = provider.get_btc_prices()
        self.assertIn("coinbase", prices)
        self.assertEqual(prices["coinbase"].source, "rest_fallback")
        self.assertEqual(prices["coinbase"].price, 47000.0)

    def test_kalshi_orderbook_uses_ws_then_rest_fallback(self) -> None:
        client = _FakeClient()
        provider = PriceProvider(
            binance_feed=None,
            coinbase_feed=None,
            kraken_feed=None,
            kalshi_feed=_FakeKalshiFeed(
                {"KXBTC15M-TEST": {"yes": [(55, 5)], "no": [(45, 5)], "source": "ws"}}
            ),
            store=_FakeStore({}),  # type: ignore[arg-type]
            client=client,  # type: ignore[arg-type]
            btc_symbol="BTCUSD",
        )
        ws_book = provider.get_kalshi_orderbook("KXBTC15M-TEST")
        self.assertIsNotNone(ws_book)
        assert ws_book is not None
        self.assertEqual(ws_book.get("source"), "ws")

        rest_book = provider.get_kalshi_orderbook("KXBTC15M-OTHER")
        self.assertIsNotNone(rest_book)
        assert rest_book is not None
        self.assertEqual(rest_book.get("source"), "rest")
        self.assertEqual(client.orderbook_calls, ["KXBTC15M-OTHER"])


if __name__ == "__main__":
    unittest.main()
