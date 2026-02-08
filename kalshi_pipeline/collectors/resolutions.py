from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

import requests

from ..kalshi_client import KalshiClient
from ..models import MarketResolution

logger = logging.getLogger(__name__)


NWS_CLI_NYC_URL = (
    "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"
)
NYC_TZ = ZoneInfo("America/New_York")


def fetch_nws_cli_nyc_max_temp(
    *, session: requests.Session | None = None
) -> dict[str, Any] | None:
    client = session or requests.Session()
    response = client.get(
        NWS_CLI_NYC_URL,
        headers={"User-Agent": "KalshiBot/1.0 (education project)"},
        timeout=20,
    )
    response.raise_for_status()
    text = response.text
    max_match = re.search(
        r"MAXIMUM TEMPERATURE.*?TODAY\s+(-?\d+)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not max_match:
        return None
    return {
        "max_temp_f": int(max_match.group(1)),
        "source": "nws_cli",
        "raw_excerpt": max_match.group(0)[:200],
    }


def _infer_market_type(series_ticker: str | None, ticker: str) -> str:
    series = (series_ticker or "").upper()
    normalized_ticker = ticker.upper()
    if series == "KXHIGHNY" or normalized_ticker.startswith("KXHIGHNY"):
        return "weather"
    if series == "KXBTC15M" or normalized_ticker.startswith("KXBTC15M"):
        return "btc_15m"
    return "unknown"


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


def _parse_kxhighny_target_date(ticker: str) -> date | None:
    match = re.search(r"KXHIGHNY-(\d{2}[A-Z]{3}\d{2})-", ticker.upper())
    if not match:
        return None
    token = match.group(1)
    try:
        return datetime.strptime(token, "%y%b%d").date()
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _weather_bounds_from_market_row(row: dict[str, Any]) -> tuple[float | None, float | None] | None:
    floor = _as_float(row.get("floor_strike") or row.get("floor"))
    cap = _as_float(row.get("cap_strike") or row.get("cap"))
    if floor is not None or cap is not None:
        return floor, cap

    text_candidates = [
        str(row.get("subtitle", "")),
        str(row.get("yes_sub_title", "")),
        str(row.get("title", "")),
    ]
    for raw_text in text_candidates:
        text = raw_text.lower()
        below_match = re.search(r"below\s+(-?\d+(?:\.\d+)?)", text)
        if below_match:
            return None, float(below_match.group(1))
        above_match = re.search(
            r"(?:above|at least|or above|and above)\s+(-?\d+(?:\.\d+)?)",
            text,
        )
        if above_match:
            return float(above_match.group(1)), None
        plus_match = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:\+|or\s+higher)", text)
        if plus_match:
            return float(plus_match.group(1)), None
        range_match = re.search(
            r"(-?\d+(?:\.\d+)?)\s*(?:to|through|-|–)\s*(-?\d+(?:\.\d+)?)",
            text,
        )
        if range_match:
            lower = float(range_match.group(1))
            upper = float(range_match.group(2))
            if lower.is_integer() and upper.is_integer():
                return lower, upper + 1.0
            return lower, upper
    return None


def _result_for_bounds(temp_f: float, bounds: tuple[float | None, float | None] | None) -> str | None:
    if bounds is None:
        return None
    lower, upper = bounds
    if lower is not None and temp_f < lower:
        return "no"
    if upper is not None and temp_f >= upper:
        return "no"
    return "yes"


def _enrich_weather_rows_with_nws(
    rows: list[MarketResolution],
    *,
    weather_bounds: dict[str, tuple[float | None, float | None] | None],
    now_utc: datetime,
) -> None:
    has_weather = any(row.market_type == "weather" for row in rows)
    if not has_weather:
        return
    try:
        nws_payload = fetch_nws_cli_nyc_max_temp()
    except requests.RequestException:
        logger.warning("nws_cli_fetch_failed", exc_info=True)
        return
    if not nws_payload:
        return
    max_temp_f = _as_float(nws_payload.get("max_temp_f"))
    if max_temp_f is None:
        return
    today_nyc = now_utc.astimezone(NYC_TZ).date()
    for idx, row in enumerate(rows):
        if row.market_type != "weather":
            continue
        market_date = _parse_kxhighny_target_date(row.ticker)
        if market_date is None or market_date != today_nyc:
            continue
        inferred_result = _result_for_bounds(max_temp_f, weather_bounds.get(row.ticker))
        rows[idx] = MarketResolution(
            ticker=row.ticker,
            series_ticker=row.series_ticker,
            event_ticker=row.event_ticker,
            market_type=row.market_type,
            resolved_at=row.resolved_at,
            result=row.result if row.result else inferred_result,
            actual_value=max_temp_f,
            resolution_source="kalshi_api+nws_cli",
            collected_at=row.collected_at,
        )


def _discover_resolution_candidates(
    client: KalshiClient,
    *,
    base_url_override: str | None,
    target_series_tickers: list[str],
    seed_tickers: list[str],
    now_utc: datetime,
    lookback_hours: int,
    max_pages_per_series: int = 4,
    page_limit: int = 200,
) -> list[str]:
    lookback_start = now_utc - timedelta(hours=max(1, lookback_hours))
    candidates: dict[str, datetime | None] = {}
    for ticker in seed_tickers:
        cleaned = str(ticker).strip()
        if cleaned:
            candidates[cleaned] = None

    for series in target_series_tickers:
        series_ticker = str(series).strip().upper()
        if not series_ticker:
            continue
        cursor: str | None = None
        pages_seen = 0
        while pages_seen < max_pages_per_series:
            params: dict[str, Any] = {"series_ticker": series_ticker, "limit": page_limit}
            if cursor:
                params["cursor"] = cursor
            try:
                payload = client._request_json(  # noqa: SLF001
                    "GET",
                    "/trade-api/v2/markets",
                    params=params,
                    base_url_override=base_url_override,
                )
            except requests.RequestException:
                logger.warning(
                    "resolution_discovery_failed series=%s", series_ticker, exc_info=True
                )
                break

            rows = payload.get("markets") or payload.get("data") or []
            if not rows:
                break
            for row in rows:
                ticker = str(row.get("ticker", "")).strip()
                if not ticker:
                    continue
                status = str(row.get("status", "")).lower()
                close_time = _parse_iso_datetime(
                    row.get("close_time") or row.get("expiration_time")
                )
                if status == "settled":
                    candidates[ticker] = close_time
                    continue
                if close_time is not None and lookback_start <= close_time <= now_utc:
                    candidates[ticker] = close_time

            pages_seen += 1
            cursor = payload.get("cursor")
            if not cursor:
                break

    floor = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        candidates.keys(),
        key=lambda ticker: candidates.get(ticker) or floor,
        reverse=True,
    )


def collect_market_resolutions(
    client: KalshiClient,
    market_tickers: list[str],
    *,
    target_series_tickers: list[str] | None = None,
    base_url_override: str | None = None,
    now_utc: datetime | None = None,
    lookback_hours: int = 48,
    max_candidates: int = 250,
) -> list[MarketResolution]:
    collected_at = now_utc or datetime.now(timezone.utc)
    weather_bounds_by_ticker: dict[str, tuple[float | None, float | None] | None] = {}
    candidates = _discover_resolution_candidates(
        client,
        base_url_override=base_url_override,
        target_series_tickers=target_series_tickers or [],
        seed_tickers=market_tickers,
        now_utc=collected_at,
        lookback_hours=lookback_hours,
    )
    if max_candidates > 0:
        candidates = candidates[:max_candidates]
    rows: list[MarketResolution] = []
    for ticker in candidates:
        try:
            payload = client._request_json(  # noqa: SLF001 - internal helper is already used across app
                "GET",
                f"/trade-api/v2/markets/{ticker}",
                base_url_override=base_url_override,
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.warning("resolution_fetch_failed ticker=%s status=%s", ticker, status)
            continue
        except requests.RequestException:
            logger.warning("resolution_fetch_failed ticker=%s", ticker, exc_info=True)
            continue

        market = payload.get("market", payload) if isinstance(payload, dict) else {}
        status = str(market.get("status", "")).lower()
        if status != "settled":
            continue

        result = market.get("result")
        resolved_at = _parse_iso_datetime(market.get("settled_time") or market.get("close_time"))

        actual_value = None
        for key in ("settlement_value", "final_value", "strike_value", "underlying_price"):
            value = market.get(key)
            if value is None:
                continue
            try:
                actual_value = float(value)
                break
            except (TypeError, ValueError):
                continue

        series_ticker = market.get("series_ticker")
        inferred_type = _infer_market_type(
            str(series_ticker) if series_ticker else None, str(ticker)
        )
        if inferred_type == "weather":
            weather_bounds_by_ticker[str(market.get("ticker") or ticker)] = (
                _weather_bounds_from_market_row(market)
            )
        rows.append(
            MarketResolution(
                ticker=str(market.get("ticker") or ticker),
                series_ticker=str(series_ticker) if series_ticker else None,
                event_ticker=str(market.get("event_ticker"))
                if market.get("event_ticker")
                else None,
                market_type=inferred_type,
                resolved_at=resolved_at,
                result=str(result).lower() if result is not None else None,
                actual_value=actual_value,
                resolution_source="kalshi_api",
                collected_at=collected_at,
            )
        )
    _enrich_weather_rows_with_nws(
        rows,
        weather_bounds=weather_bounds_by_ticker,
        now_utc=collected_at,
    )
    return rows
