from __future__ import annotations

import base64
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ModuleNotFoundError:  # pragma: no cover - runtime guard for missing deps
    hashes = None
    serialization = None
    padding = None
import requests

from .config import Settings
from .mock_data import (
    generate_current_snapshot,
    generate_historical_snapshots,
    generate_markets,
)
from .models import Market, MarketSnapshot

logger = logging.getLogger(__name__)


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tokenize_group(group: str) -> list[str]:
    raw_tokens = re.findall(r"[a-z0-9]+", group.lower())
    stopwords = {"the", "a", "an", "in", "for", "at", "to", "of", "will", "be"}
    return [token for token in raw_tokens if token not in stopwords]


def _market_text(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("ticker", "")),
        str(row.get("title", "")),
        str(row.get("subtitle", "")),
        str(row.get("event_ticker", "")),
        str(row.get("series_ticker", "")),
    ]
    return " ".join(parts).lower()


class KalshiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self._private_key = None

    def health_check(self) -> dict[str, Any]:
        if self.settings.kalshi_stub_mode:
            return {"ok": True, "mode": "stub"}
        if self.settings.kalshi_use_auth_for_public_data:
            payload = self._request_json(
                "GET", "/trade-api/v2/portfolio/balance", require_auth=True
            )
            return {"ok": True, "mode": "live-auth", "result_keys": sorted(payload.keys())}
        payload = self._request_json("GET", "/trade-api/v2/markets", params={"limit": 1})
        return {"ok": True, "mode": "live-public", "result_keys": sorted(payload.keys())}

    def list_markets(self, limit: int) -> list[Market]:
        if self.settings.kalshi_stub_mode:
            return generate_markets(limit)
        if self.settings.target_market_tickers:
            rows = self._fetch_markets_by_ticker(self.settings.target_market_tickers[:limit])
        else:
            rows = self._discover_target_markets(limit=limit)
            if self.settings.auto_select_live_contracts:
                rows = self._select_live_contract_rows(rows)
        markets: list[Market] = []
        for row in rows:
            ticker = row.get("ticker") or row.get("id")
            if not ticker:
                continue
            markets.append(
                Market(
                    ticker=str(ticker),
                    title=str(row.get("title", ticker)),
                    status=str(row.get("status", "unknown")),
                    close_time=_parse_iso_datetime(row.get("close_time") or row.get("expiration_time")),
                    raw_json=row,
                )
            )
        return markets

    def get_current_snapshot(self, market: Market) -> MarketSnapshot:
        if self.settings.kalshi_stub_mode:
            return generate_current_snapshot(market, datetime.now(timezone.utc))
        payload = self._request_json("GET", f"/trade-api/v2/markets/{market.ticker}")
        payload = payload.get("market", payload)
        yes_price = _as_float(
            payload.get("yes_ask")
            or payload.get("yes_bid")
            or payload.get("yes_price")
            or payload.get("last_price")
        )
        no_price = _as_float(payload.get("no_ask") or payload.get("no_bid") or payload.get("no_price"))
        if yes_price is not None and no_price is None:
            no_price = max(0.0, round(1 - yes_price, 3))
        return MarketSnapshot(
            ticker=market.ticker,
            ts=datetime.now(timezone.utc),
            yes_price=yes_price,
            no_price=no_price,
            volume=_as_float(payload.get("volume")),
            raw_json=payload,
        )

    def get_historical_snapshots(
        self, market: Market, start: datetime, end: datetime
    ) -> list[MarketSnapshot]:
        if self.settings.kalshi_stub_mode:
            return generate_historical_snapshots(market, start, end)
        series_ticker = self._series_from_ticker(market)
        attempts: list[tuple[str, dict[str, Any]]] = [
            (
                f"/trade-api/v2/markets/{market.ticker}/candlesticks",
                {"start": start.isoformat(), "end": end.isoformat(), "period_interval": 60},
            ),
        ]
        if series_ticker:
            attempts.append(
                (
                    f"/trade-api/v2/markets/{series_ticker}/{market.ticker}/candlesticks",
                    {
                        "start_ts": int(start.timestamp()),
                        "end_ts": int(end.timestamp()),
                        "period_interval": 1,
                    },
                )
            )

        payload: dict[str, Any] | None = None
        for path, params in attempts:
            try:
                payload = self._request_json("GET", path, params=params)
                break
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status in {404, 400}:
                    continue
                raise
        if payload is None:
            logger.info("No candlestick endpoint available for ticker=%s", market.ticker)
            return []

        rows = payload.get("candlesticks") or payload.get("candles") or payload.get("data") or []
        snapshots: list[MarketSnapshot] = []
        for row in rows:
            ts = _parse_iso_datetime(
                row.get("end_period_ts") or row.get("end_ts") or row.get("ts")
            )
            if ts is None:
                continue
            yes_price = _as_float(row.get("close_yes") or row.get("yes_price") or row.get("close"))
            no_price = _as_float(row.get("close_no") or row.get("no_price"))
            if yes_price is not None and no_price is None:
                no_price = max(0.0, round(1 - yes_price, 3))
            snapshots.append(
                MarketSnapshot(
                    ticker=market.ticker,
                    ts=ts,
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=_as_float(row.get("volume")),
                    raw_json=row,
                )
            )
        return snapshots

    def _fetch_markets_by_ticker(self, tickers: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ticker in tickers:
            payload = self._request_json("GET", f"/trade-api/v2/markets/{ticker}")
            row = payload.get("market", payload)
            if not isinstance(row, dict):
                continue
            if not row.get("ticker"):
                row["ticker"] = ticker
            rows.append(row)
        return rows

    def _discover_target_markets(self, limit: int) -> list[dict[str, Any]]:
        base_params: dict[str, Any] = {"limit": min(1000, max(1, limit))}
        if self.settings.target_market_status:
            base_params["status"] = self.settings.target_market_status

        search_param_sets: list[dict[str, Any]]
        if self.settings.target_series_tickers:
            search_param_sets = [
                {**base_params, "series_ticker": series}
                for series in self.settings.target_series_tickers
            ]
        else:
            search_param_sets = [base_params]

        matched: dict[str, dict[str, Any]] = {}
        for params in search_param_sets:
            current_series_filter = str(params.get("series_ticker", "")).upper()
            pages_seen = 0
            cursor: str | None = None
            while pages_seen < self.settings.target_market_discovery_pages:
                page_params = dict(params)
                if cursor:
                    page_params["cursor"] = cursor
                payload = self._request_json("GET", "/trade-api/v2/markets", params=page_params)
                rows = payload.get("markets") or payload.get("data") or []
                if not rows:
                    break
                for row in rows:
                    ticker = str(row.get("ticker", "")).strip()
                    if not ticker:
                        continue
                    # If the query is already scoped by series_ticker, trust API scope.
                    # Some responses may omit series_ticker in each row, so backfill it.
                    if current_series_filter:
                        row_series = str(row.get("series_ticker", "")).upper()
                        if row_series and row_series != current_series_filter:
                            continue
                        if not row_series:
                            row["series_ticker"] = current_series_filter
                        matched[ticker] = row
                        continue

                    if not self._matches_targets(row):
                        continue
                    matched[ticker] = row
                pages_seen += 1
                cursor = payload.get("cursor")
                if not cursor:
                    break

        # Fallback: if status filter returns no rows, retry once without status.
        if not matched and self.settings.target_market_status:
            logger.info(
                "No markets matched with status=%s; retrying discovery without status filter",
                self.settings.target_market_status,
            )
            base_params_no_status: dict[str, Any] = {"limit": min(1000, max(1, limit))}
            if self.settings.target_series_tickers:
                fallback_param_sets = [
                    {**base_params_no_status, "series_ticker": series}
                    for series in self.settings.target_series_tickers
                ]
            else:
                fallback_param_sets = [base_params_no_status]
            for params in fallback_param_sets:
                current_series_filter = str(params.get("series_ticker", "")).upper()
                pages_seen = 0
                cursor: str | None = None
                while pages_seen < self.settings.target_market_discovery_pages:
                    page_params = dict(params)
                    if cursor:
                        page_params["cursor"] = cursor
                    payload = self._request_json("GET", "/trade-api/v2/markets", params=page_params)
                    rows = payload.get("markets") or payload.get("data") or []
                    if not rows:
                        break
                    for row in rows:
                        ticker = str(row.get("ticker", "")).strip()
                        if not ticker:
                            continue
                        if current_series_filter:
                            row_series = str(row.get("series_ticker", "")).upper()
                            if row_series and row_series != current_series_filter:
                                continue
                            if not row_series:
                                row["series_ticker"] = current_series_filter
                            matched[ticker] = row
                            continue
                        if not self._matches_targets(row):
                            continue
                        matched[ticker] = row
                    pages_seen += 1
                    cursor = payload.get("cursor")
                    if not cursor:
                        break

        if matched:
            return list(matched.values())[:limit]
        return []

    def _select_live_contract_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        now = datetime.now(timezone.utc)

        weather_rows = [
            row
            for row in rows
            if str(row.get("series_ticker", "")).upper() == "KXHIGHNY"
            or str(row.get("ticker", "")).upper().startswith("KXHIGHNY")
        ]
        btc_rows = [
            row
            for row in rows
            if str(row.get("series_ticker", "")).upper() == "KXBTC15M"
            or str(row.get("ticker", "")).upper().startswith("KXBTC15M")
        ]

        selected: list[dict[str, Any]] = []

        if weather_rows:
            by_event: dict[str, list[dict[str, Any]]] = {}
            for row in weather_rows:
                event_key = str(row.get("event_ticker") or row.get("event") or "").strip()
                if not event_key:
                    event_key = str(row.get("ticker", "")).split("-")[0]
                by_event.setdefault(event_key, []).append(row)

            def event_sort_key(event_rows: list[dict[str, Any]]) -> tuple[int, float]:
                close_times = [self._row_close_time(row) for row in event_rows]
                known_times = [ts for ts in close_times if ts is not None]
                if not known_times:
                    return (2, float("inf"))
                # event close proxy: latest close in that bracket set
                event_close = max(known_times)
                if event_close >= now:
                    return (0, event_close.timestamp())
                return (1, -event_close.timestamp())  # most recent past event last

            best_event_rows = min(by_event.values(), key=event_sort_key)
            selected.extend(best_event_rows)

        if btc_rows:
            future = [row for row in btc_rows if (self._row_close_time(row) or now) >= now]
            if future:
                best_btc = min(
                    future,
                    key=lambda row: self._row_close_time(row) or datetime.max.replace(tzinfo=timezone.utc),
                )
            else:
                best_btc = max(
                    btc_rows,
                    key=lambda row: self._row_close_time(row) or datetime.min.replace(tzinfo=timezone.utc),
                )
            selected.append(best_btc)

        if not selected:
            return rows
        unique_by_ticker: dict[str, dict[str, Any]] = {}
        for row in selected:
            ticker = str(row.get("ticker", "")).strip()
            if not ticker:
                continue
            unique_by_ticker[ticker] = row
        return list(unique_by_ticker.values())

    def _row_close_time(self, row: dict[str, Any]) -> datetime | None:
        return _parse_iso_datetime(row.get("close_time") or row.get("expiration_time"))

    def _series_from_ticker(self, market: Market) -> str:
        raw_series = str(market.raw_json.get("series_ticker", "")).strip().upper()
        if raw_series:
            return raw_series
        ticker = market.ticker.strip().upper()
        if "-" in ticker:
            return ticker.split("-", 1)[0]
        return ""

    def _matches_targets(self, row: dict[str, Any]) -> bool:
        ticker = str(row.get("ticker", "")).upper()
        event_ticker = str(row.get("event_ticker", "")).upper()
        series_ticker = str(row.get("series_ticker", "")).upper()
        if self.settings.target_event_tickers and event_ticker in self.settings.target_event_tickers:
            return True
        if self.settings.target_series_tickers and series_ticker in self.settings.target_series_tickers:
            return True
        text = _market_text(row)
        for group in self.settings.target_market_query_groups:
            tokens = _tokenize_group(group)
            if not tokens:
                continue
            if all(token in text for token in tokens):
                return True
        # keep explicit fallback support even in discovery mode
        if self.settings.target_market_tickers and ticker in self.settings.target_market_tickers:
            return True
        return False

    def _request_json(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        require_auth: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.settings.kalshi_base_url.rstrip('/')}{path}"
        split = urlsplit(url)
        path_for_signing = split.path or path
        headers = {"Accept": "application/json"}
        should_authenticate = require_auth or self.settings.kalshi_use_auth_for_public_data
        if should_authenticate:
            headers.update(self._build_auth_headers(method=method, path=path_for_signing))
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    def _build_auth_headers(self, method: str, path: str) -> dict[str, str]:
        if hashes is None or serialization is None or padding is None:
            raise RuntimeError(
                "Missing dependency 'cryptography'. Install requirements before live mode."
            )
        has_key_material = bool(
            self.settings.kalshi_api_key_secret or self.settings.kalshi_private_key_path
        )
        if not self.settings.kalshi_api_key_id or not has_key_material:
            raise RuntimeError(
                "KALSHI_API_KEY_ID and private key material are required for live mode."
            )
        private_key = self._load_private_key()
        timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        signature_b64 = base64.b64encode(signature).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY": self.settings.kalshi_api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
        }

    def _load_private_key(self):
        if self._private_key is not None:
            return self._private_key

        raw_key = ""
        if self.settings.kalshi_private_key_path:
            key_path = Path(self.settings.kalshi_private_key_path)
            raw_key = key_path.read_text(encoding="utf-8")
        elif self.settings.kalshi_api_key_secret:
            possible_path = Path(self.settings.kalshi_api_key_secret)
            if possible_path.exists() and possible_path.is_file():
                raw_key = possible_path.read_text(encoding="utf-8")
            else:
                raw_key = self.settings.kalshi_api_key_secret
        raw_key = raw_key.strip()
        if not raw_key:
            raise RuntimeError(
                "No private key found. Set KALSHI_PRIVATE_KEY_PATH or KALSHI_API_KEY_SECRET."
            )
        if "\\n" in raw_key and "-----BEGIN" in raw_key:
            raw_key = raw_key.replace("\\n", "\n")
        password_raw = os.getenv("KALSHI_PRIVATE_KEY_PASSWORD", "")
        password: bytes | None = password_raw.encode("utf-8") if password_raw else None
        self._private_key = serialization.load_pem_private_key(
            raw_key.encode("utf-8"), password=password
        )
        return self._private_key
