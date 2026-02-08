from __future__ import annotations

from datetime import datetime, timezone

import requests

from ..config import Settings
from ..models import CryptoSpotTick


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_btc_spot_ticks(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    now_utc: datetime | None = None,
) -> list[CryptoSpotTick]:
    current_utc = now_utc or datetime.now(timezone.utc)
    client = session or requests.Session()
    ticks: list[CryptoSpotTick] = []

    # Binance does not offer BTC-USD directly with the same liquidity as BTCUSDT.
    binance_resp = client.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": "BTCUSDT"},
        timeout=10,
    )
    binance_resp.raise_for_status()
    binance_payload = binance_resp.json()
    binance_price = _as_float(binance_payload.get("price"))
    if binance_price is not None:
        ticks.append(
            CryptoSpotTick(
                ts=current_utc,
                source="binance",
                symbol=settings.btc_symbol,
                price_usd=binance_price,
                raw_json=binance_payload if isinstance(binance_payload, dict) else {},
            )
        )

    coinbase_resp = client.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        timeout=10,
    )
    coinbase_resp.raise_for_status()
    coinbase_payload = coinbase_resp.json()
    coinbase_price = _as_float(coinbase_payload.get("price"))
    if coinbase_price is not None:
        ticks.append(
            CryptoSpotTick(
                ts=current_utc,
                source="coinbase",
                symbol=settings.btc_symbol,
                price_usd=coinbase_price,
                raw_json=coinbase_payload if isinstance(coinbase_payload, dict) else {},
            )
        )

    kraken_resp = client.get(
        "https://api.kraken.com/0/public/Ticker",
        params={"pair": "XBTUSD"},
        timeout=10,
    )
    kraken_resp.raise_for_status()
    kraken_payload = kraken_resp.json()
    kraken_price = None
    if isinstance(kraken_payload, dict):
        result = kraken_payload.get("result", {})
        if isinstance(result, dict):
            for value in result.values():
                if not isinstance(value, dict):
                    continue
                close_values = value.get("c")
                if isinstance(close_values, list) and close_values:
                    kraken_price = _as_float(close_values[0])
                    if kraken_price is not None:
                        break
    if kraken_price is not None:
        ticks.append(
            CryptoSpotTick(
                ts=current_utc,
                source="kraken",
                symbol=settings.btc_symbol,
                price_usd=kraken_price,
                raw_json=kraken_payload if isinstance(kraken_payload, dict) else {},
            )
        )

    return ticks

