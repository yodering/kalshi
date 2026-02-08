from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _as_float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


def _as_list(value: str | None) -> list[str]:
    if value is None:
        return []
    items = []
    for part in value.split(","):
        cleaned = part.strip()
        if cleaned:
            items.append(cleaned)
    return items


def _as_btc_sources(value: str | None, default: list[str]) -> list[str]:
    allowed = {"binance", "coinbase", "kraken", "bitstamp"}
    raw_items = _as_list(value) if value is not None else list(default)
    normalized: list[str] = []
    for item in raw_items:
        source = item.strip().lower()
        if source not in allowed:
            continue
        if source in normalized:
            continue
        normalized.append(source)
    return normalized


def _as_market_ids(value: str | None) -> list[str]:
    if value is None:
        return []
    items: list[str] = []
    for raw_part in value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        # Allow full Kalshi URLs and extract the last path segment as ticker.
        if "/" in part:
            parsed = urlsplit(part)
            path = parsed.path.strip("/")
            if path:
                part = path.split("/")[-1]
        items.append(part.upper())
    return items


def _as_groups(value: str | None) -> list[str]:
    if value is None:
        return []
    groups = []
    for part in value.split(";"):
        cleaned = part.strip()
        if cleaned:
            groups.append(cleaned)
    return groups


def _as_market_status(value: str | None, default: str = "open") -> str:
    if value is None:
        return default
    cleaned = value.strip()
    if not cleaned:
        return default
    if cleaned.lower() in {"any", "all", "none", "*"}:
        return ""
    return cleaned


def _clean_env(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().strip('"').strip("'")


def _is_unresolved_template(value: str) -> bool:
    return "${{" in value or "}}" in value


def _build_database_url_from_parts() -> tuple[str, str] | None:
    host = _clean_env(os.getenv("PGHOST") or os.getenv("POSTGRES_HOST"))
    port = _clean_env(os.getenv("PGPORT") or os.getenv("POSTGRES_PORT"))
    user = _clean_env(os.getenv("PGUSER") or os.getenv("POSTGRES_USER"))
    password = _clean_env(os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD"))
    database = _clean_env(os.getenv("PGDATABASE") or os.getenv("POSTGRES_DB"))
    if not all([host, port, user, password, database]):
        return None
    built = (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@"
        f"{host}:{port}/{quote_plus(database)}"
    )
    return built, "PG* parts"


def _add_sslmode_require_if_needed(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "sslmode" in query:
        return url
    # Public Railway Postgres endpoints generally require SSL.
    if host.endswith(".railway.app"):
        query["sslmode"] = "require"
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )
    return url


def _normalize_kalshi_base_url(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return "https://api.elections.kalshi.com"
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    netloc = parts.netloc

    # Historical/default Kalshi production host for Trade API endpoints.
    if host == "api.kalshi.com":
        host_port = "api.elections.kalshi.com"
        if parts.port:
            host_port = f"{host_port}:{parts.port}"
        netloc = host_port

    # Users sometimes paste full REST root; client appends /trade-api/v2 paths.
    path = parts.path or ""
    if path.endswith("/trade-api/v2"):
        path = path[: -len("/trade-api/v2")]
    if path.endswith("/trade-api/v2/"):
        path = path[: -len("/trade-api/v2/")]
    path = path.rstrip("/")

    return urlunsplit((parts.scheme or "https", netloc, path, "", ""))


def resolve_database_url() -> tuple[str, str]:
    key_order = [
        "DATABASE_URL",
        "DATABASE_PRIVATE_URL",
        "POSTGRES_URL",
        "POSTGRESQL_URL",
        "DATABASE_PUBLIC_URL",
    ]
    for key in key_order:
        candidate = _clean_env(os.getenv(key))
        if not candidate:
            continue
        if _is_unresolved_template(candidate):
            continue
        if ".railway.internal" in candidate:
            public_fallback_order = [
                "DATABASE_PUBLIC_URL",
                "POSTGRES_PUBLIC_URL",
                "POSTGRES_URL_NON_POOLING",
                "PG_URL",
            ]
            for fallback_key in public_fallback_order:
                fallback = _clean_env(os.getenv(fallback_key))
                if not fallback or _is_unresolved_template(fallback):
                    continue
                if ".railway.internal" in fallback:
                    continue
                return _add_sslmode_require_if_needed(fallback), f"{key}->{fallback_key}"
        return _add_sslmode_require_if_needed(candidate), key

    built = _build_database_url_from_parts()
    if built is not None:
        return _add_sslmode_require_if_needed(built[0]), built[1]

    return "postgresql://postgres:postgres@localhost:5432/kalshi", "default-local"


def redact_database_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "unknown-host"
    port = parts.port
    database = parts.path.lstrip("/") or "unknown-db"
    host_port = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme or "postgresql", host_port, f"/{database}", "", ""))


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_url_source: str
    poll_interval_seconds: int
    market_limit: int
    historical_days: int
    historical_markets: int
    run_historical_backfill_on_start: bool
    kalshi_stub_mode: bool
    kalshi_base_url: str
    kalshi_use_auth_for_public_data: bool
    kalshi_api_key_id: str
    kalshi_api_key_secret: str
    kalshi_private_key_path: str
    target_market_tickers: list[str]
    target_event_tickers: list[str]
    target_series_tickers: list[str]
    auto_select_live_contracts: bool
    target_market_query_groups: list[str]
    target_market_status: str
    target_market_discovery_pages: int
    store_raw_json: bool
    weather_enabled: bool
    weather_latitude: float
    weather_longitude: float
    weather_timezone: str
    weather_ensemble_models: list[str]
    weather_forecast_days: int
    btc_enabled: bool
    btc_symbol: str
    btc_enabled_sources: list[str]
    btc_core_sources: list[str]
    btc_min_core_sources: int
    btc_momentum_lookback_minutes: int
    signal_min_edge_bps: int
    signal_store_all: bool

    @classmethod
    def from_env(cls) -> "Settings":
        database_url, database_url_source = resolve_database_url()
        btc_enabled_sources = _as_btc_sources(
            os.getenv("BTC_ENABLED_SOURCES"),
            ["coinbase", "kraken", "bitstamp"],
        )
        if not btc_enabled_sources:
            btc_enabled_sources = ["coinbase", "kraken", "bitstamp"]

        btc_core_sources = _as_btc_sources(
            os.getenv("BTC_CORE_SOURCES"),
            ["coinbase", "kraken", "bitstamp"],
        )
        btc_core_sources = [source for source in btc_core_sources if source in btc_enabled_sources]
        if not btc_core_sources:
            btc_core_sources = [source for source in btc_enabled_sources if source != "binance"]
        if not btc_core_sources:
            btc_core_sources = list(btc_enabled_sources)

        btc_min_core_sources = _as_int(os.getenv("BTC_MIN_CORE_SOURCES"), 2)
        if btc_min_core_sources < 1:
            btc_min_core_sources = 1
        if btc_min_core_sources > len(btc_core_sources):
            btc_min_core_sources = len(btc_core_sources)

        return cls(
            database_url=database_url,
            database_url_source=database_url_source,
            poll_interval_seconds=_as_int(os.getenv("POLL_INTERVAL_SECONDS"), 300),
            market_limit=_as_int(os.getenv("MARKET_LIMIT"), 25),
            historical_days=_as_int(os.getenv("HISTORICAL_DAYS"), 7),
            historical_markets=_as_int(os.getenv("HISTORICAL_MARKETS"), 10),
            run_historical_backfill_on_start=_as_bool(
                os.getenv("RUN_HISTORICAL_BACKFILL_ON_START"), True
            ),
            kalshi_stub_mode=_as_bool(os.getenv("KALSHI_STUB_MODE"), True),
            kalshi_base_url=_normalize_kalshi_base_url(os.getenv("KALSHI_BASE_URL")),
            kalshi_use_auth_for_public_data=_as_bool(
                os.getenv("KALSHI_USE_AUTH_FOR_PUBLIC_DATA"), False
            ),
            kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
            kalshi_api_key_secret=os.getenv("KALSHI_API_KEY_SECRET", ""),
            kalshi_private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH", ""),
            target_market_tickers=_as_market_ids(os.getenv("TARGET_MARKET_TICKERS")),
            target_event_tickers=_as_market_ids(os.getenv("TARGET_EVENT_TICKERS")),
            target_series_tickers=_as_market_ids(
                os.getenv("TARGET_SERIES_TICKERS", "KXHIGHNY,KXBTC15M")
            ),
            auto_select_live_contracts=_as_bool(
                os.getenv("AUTO_SELECT_LIVE_CONTRACTS"), True
            ),
            target_market_query_groups=_as_groups(
                os.getenv(
                    "TARGET_MARKET_QUERY_GROUPS",
                    "highest temperature in nyc today;bitcoin price up down 15 minutes",
                )
            ),
            target_market_status=_as_market_status(os.getenv("TARGET_MARKET_STATUS"), "open"),
            target_market_discovery_pages=_as_int(os.getenv("TARGET_MARKET_DISCOVERY_PAGES"), 10),
            store_raw_json=_as_bool(os.getenv("STORE_RAW_JSON"), False),
            weather_enabled=_as_bool(os.getenv("WEATHER_ENABLED"), True),
            weather_latitude=_as_float(os.getenv("WEATHER_LATITUDE"), 40.7829),
            weather_longitude=_as_float(os.getenv("WEATHER_LONGITUDE"), -73.9654),
            weather_timezone=os.getenv("WEATHER_TIMEZONE", "America/New_York").strip()
            or "America/New_York",
            weather_ensemble_models=_as_list(
                os.getenv("WEATHER_ENSEMBLE_MODELS", "gfs_ensemble,ecmwf_ifs025_ensemble")
            ),
            weather_forecast_days=_as_int(os.getenv("WEATHER_FORECAST_DAYS"), 2),
            btc_enabled=_as_bool(os.getenv("BTC_ENABLED"), True),
            btc_symbol=os.getenv("BTC_SYMBOL", "BTCUSD").strip() or "BTCUSD",
            btc_enabled_sources=btc_enabled_sources,
            btc_core_sources=btc_core_sources,
            btc_min_core_sources=btc_min_core_sources,
            btc_momentum_lookback_minutes=_as_int(
                os.getenv("BTC_MOMENTUM_LOOKBACK_MINUTES"), 5
            ),
            signal_min_edge_bps=_as_int(os.getenv("SIGNAL_MIN_EDGE_BPS"), 150),
            signal_store_all=_as_bool(os.getenv("SIGNAL_STORE_ALL"), True),
        )
