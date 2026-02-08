from __future__ import annotations

from datetime import datetime, timezone
import logging

import requests

from ..config import Settings
from ..models import CryptoSpotTick

logger = logging.getLogger(__name__)


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_binance(
    client: requests.Session, settings: Settings, current_utc: datetime
) -> CryptoSpotTick | None:
    response = client.get(
        "https://api.binance.com/api/v3/ticker/price",
        params={"symbol": "BTCUSDT"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    price = _as_float(payload.get("price"))
    if price is None:
        return None
    return CryptoSpotTick(
        ts=current_utc,
        source="binance",
        symbol=settings.btc_symbol,
        price_usd=price,
        raw_json=payload if isinstance(payload, dict) else {},
    )


def _fetch_coinbase(
    client: requests.Session, settings: Settings, current_utc: datetime
) -> CryptoSpotTick | None:
    response = client.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    price = _as_float(payload.get("price"))
    if price is None:
        return None
    return CryptoSpotTick(
        ts=current_utc,
        source="coinbase",
        symbol=settings.btc_symbol,
        price_usd=price,
        raw_json=payload if isinstance(payload, dict) else {},
    )


def _fetch_kraken(
    client: requests.Session, settings: Settings, current_utc: datetime
) -> CryptoSpotTick | None:
    response = client.get(
        "https://api.kraken.com/0/public/Ticker",
        params={"pair": "XBTUSD"},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    price = None
    if isinstance(payload, dict):
        result = payload.get("result", {})
        if isinstance(result, dict):
            for value in result.values():
                if not isinstance(value, dict):
                    continue
                close_values = value.get("c")
                if isinstance(close_values, list) and close_values:
                    price = _as_float(close_values[0])
                    if price is not None:
                        break
    if price is None:
        return None
    return CryptoSpotTick(
        ts=current_utc,
        source="kraken",
        symbol=settings.btc_symbol,
        price_usd=price,
        raw_json=payload if isinstance(payload, dict) else {},
    )


def _fetch_bitstamp(
    client: requests.Session, settings: Settings, current_utc: datetime
) -> CryptoSpotTick | None:
    response = client.get(
        "https://www.bitstamp.net/api/v2/ticker/btcusd/",
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    price = _as_float(payload.get("last"))
    if price is None:
        return None
    return CryptoSpotTick(
        ts=current_utc,
        source="bitstamp",
        symbol=settings.btc_symbol,
        price_usd=price,
        raw_json=payload if isinstance(payload, dict) else {},
    )


def fetch_btc_spot_ticks(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    now_utc: datetime | None = None,
) -> list[CryptoSpotTick]:
    current_utc = now_utc or datetime.now(timezone.utc)
    client = session or requests.Session()
    ticks: list[CryptoSpotTick] = []
    source_fetchers = {
        "binance": _fetch_binance,
        "coinbase": _fetch_coinbase,
        "kraken": _fetch_kraken,
        "bitstamp": _fetch_bitstamp,
    }
    for source in settings.btc_enabled_sources:
        fetcher = source_fetchers.get(source)
        if fetcher is None:
            continue
        try:
            tick = fetcher(client, settings, current_utc)
            if tick is not None:
                ticks.append(tick)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.warning("btc_source_failed source=%s status=%s", source, status)
        except requests.RequestException:
            logger.warning("btc_source_failed source=%s", source, exc_info=True)

    return ticks
