from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from .config import Settings
from .mock_data import (
    generate_current_snapshot,
    generate_historical_snapshots,
    generate_markets,
)
from .models import Market, MarketSnapshot


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


class KalshiClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()

    def health_check(self) -> dict[str, Any]:
        if self.settings.kalshi_stub_mode:
            return {"ok": True, "mode": "stub"}
        payload = self._request_json("GET", "/trade-api/v2/markets", params={"limit": 1})
        return {"ok": True, "mode": "live", "result_keys": sorted(payload.keys())}

    def list_markets(self, limit: int) -> list[Market]:
        if self.settings.kalshi_stub_mode:
            return generate_markets(limit)
        payload = self._request_json("GET", "/trade-api/v2/markets", params={"limit": limit})
        rows = payload.get("markets") or payload.get("data") or []
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
        params = {"start": start.isoformat(), "end": end.isoformat(), "period_interval": 60}
        payload = self._request_json(
            "GET", f"/trade-api/v2/markets/{market.ticker}/candlesticks", params=params
        )
        rows = payload.get("candlesticks") or payload.get("candles") or payload.get("data") or []
        snapshots: list[MarketSnapshot] = []
        for row in rows:
            ts = _parse_iso_datetime(row.get("end_period_ts") or row.get("ts"))
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

    def _request_json(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.settings.kalshi_base_url.rstrip('/')}{path}"
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            headers=self._build_auth_headers(),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"data": payload}

    def _build_auth_headers(self) -> dict[str, str]:
        if not self.settings.kalshi_api_key_id or not self.settings.kalshi_api_key_secret:
            raise RuntimeError(
                "KALSHI_API_KEY_ID and KALSHI_API_KEY_SECRET are required for live mode."
            )
        # Placeholder auth wiring for Week 1. Replace with official signed request auth when keys are ready.
        return {
            "X-KALSHI-KEY-ID": self.settings.kalshi_api_key_id,
            "X-KALSHI-KEY-SECRET": self.settings.kalshi_api_key_secret,
            "Accept": "application/json",
        }
