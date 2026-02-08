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


def fetch_btc_spot_ticks(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    now_utc: datetime | None = None,
) -> list[CryptoSpotTick]:
    current_utc = now_utc or datetime.now(timezone.utc)
    client = session or requests.Session()
    ticks: list[CryptoSpotTick] = []

    # Binance can be geo-restricted in some cloud regions; failure should not stop collection.
    try:
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
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        logger.warning("btc_source_failed source=binance status=%s", status)
    except requests.RequestException:
        logger.warning("btc_source_failed source=binance", exc_info=True)

    try:
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
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        logger.warning("btc_source_failed source=coinbase status=%s", status)
    except requests.RequestException:
        logger.warning("btc_source_failed source=coinbase", exc_info=True)

    try:
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
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        logger.warning("btc_source_failed source=kraken status=%s", status)
    except requests.RequestException:
        logger.warning("btc_source_failed source=kraken", exc_info=True)

    # Lightweight additional fallback.
    if not ticks:
        try:
            bitstamp_resp = client.get(
                "https://www.bitstamp.net/api/v2/ticker/btcusd/",
                timeout=10,
            )
            bitstamp_resp.raise_for_status()
            bitstamp_payload = bitstamp_resp.json()
            bitstamp_price = _as_float(bitstamp_payload.get("last"))
            if bitstamp_price is not None:
                ticks.append(
                    CryptoSpotTick(
                        ts=current_utc,
                        source="bitstamp",
                        symbol=settings.btc_symbol,
                        price_usd=bitstamp_price,
                        raw_json=bitstamp_payload if isinstance(bitstamp_payload, dict) else {},
                    )
                )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.warning("btc_source_failed source=bitstamp status=%s", status)
        except requests.RequestException:
            logger.warning("btc_source_failed source=bitstamp", exc_info=True)

    return ticks
