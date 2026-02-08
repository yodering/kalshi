from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit


TRADING_PROFILE_DEFAULTS: dict[str, dict[str, float | int]] = {
    "conservative": {
        "paper_trade_min_edge_bps": 300,
        "paper_trade_min_confidence": 0.35,
        "paper_trade_contract_count": 1,
        "paper_trade_max_orders_per_cycle": 1,
        "paper_trade_cooldown_minutes": 45,
        "paper_trade_min_price_cents": 8,
        "paper_trade_max_price_cents": 92,
        "telegram_min_edge_bps": 200,
        "signal_min_edge_bps": 200,
    },
    "balanced": {
        "paper_trade_min_edge_bps": 200,
        "paper_trade_min_confidence": 0.25,
        "paper_trade_contract_count": 2,
        "paper_trade_max_orders_per_cycle": 2,
        "paper_trade_cooldown_minutes": 30,
        "paper_trade_min_price_cents": 5,
        "paper_trade_max_price_cents": 95,
        "telegram_min_edge_bps": 150,
        "signal_min_edge_bps": 150,
    },
    "aggressive": {
        "paper_trade_min_edge_bps": 125,
        "paper_trade_min_confidence": 0.2,
        "paper_trade_contract_count": 3,
        "paper_trade_max_orders_per_cycle": 3,
        "paper_trade_cooldown_minutes": 15,
        "paper_trade_min_price_cents": 3,
        "paper_trade_max_price_cents": 97,
        "telegram_min_edge_bps": 100,
        "signal_min_edge_bps": 100,
    },
}

BOT_MODE_DEFAULTS: dict[str, dict[str, str | bool]] = {
    "custom": {},
    "demo_safe": {
        "kalshi_base_url": "https://demo-api.kalshi.co",
        "paper_trading_base_url": "https://demo-api.kalshi.co",
        "paper_trading_enabled": True,
        "paper_trading_mode": "kalshi_demo",
        "kalshi_stub_mode": False,
        "trading_profile": "conservative",
        "kalshi_key_profile": "paper",
    },
    "live_safe": {
        "kalshi_base_url": "https://api.elections.kalshi.com",
        "paper_trading_base_url": "https://api.elections.kalshi.com",
        "paper_trading_enabled": False,
        "paper_trading_mode": "kalshi_demo",
        "kalshi_stub_mode": False,
        "trading_profile": "conservative",
        "kalshi_key_profile": "real",
    },
    "live_auto": {
        "kalshi_base_url": "https://api.elections.kalshi.com",
        "paper_trading_base_url": "https://api.elections.kalshi.com",
        "paper_trading_enabled": True,
        "paper_trading_mode": "kalshi_demo",
        "kalshi_stub_mode": False,
        "trading_profile": "conservative",
        "kalshi_key_profile": "real",
    },
}


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


def _as_signal_types(value: str | None, default: list[str]) -> list[str]:
    allowed = {"weather", "btc"}
    raw_items = _as_list(value) if value is not None else list(default)
    normalized: list[str] = []
    for item in raw_items:
        signal_type = item.strip().lower()
        if signal_type not in allowed:
            continue
        if signal_type in normalized:
            continue
        normalized.append(signal_type)
    return normalized


def _as_paper_trading_mode(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"kalshi_demo", "simulate"}:
        return normalized
    return "simulate"


def _as_paper_trade_sizing_mode(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"fixed", "kelly"}:
        return normalized
    return "kelly"


def _as_bot_mode(value: str | None, default: str = "custom") -> str:
    normalized = (value or "").strip().lower()
    if normalized in BOT_MODE_DEFAULTS:
        return normalized
    return default


def _as_key_profile(value: str | None, default: str = "direct") -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"direct", "paper", "real"}:
        return normalized
    return default


def _as_trading_profile(value: str | None, default: str = "balanced") -> str:
    normalized = (value or "").strip().lower()
    if normalized in TRADING_PROFILE_DEFAULTS:
        return normalized
    return default


def _resolve_kalshi_credentials(
    key_profile: str,
) -> tuple[str, str, str]:
    if key_profile == "paper":
        key_id = (
            os.getenv("KALSHI_PAPER_API_KEY_ID", "").strip()
            or os.getenv("KALSHI_API_KEY_ID", "").strip()
        )
        key_secret = (
            os.getenv("KALSHI_PAPER_API_KEY_SECRET", "").strip()
            or os.getenv("KALSHI_API_KEY_SECRET", "").strip()
        )
        key_path = (
            os.getenv("KALSHI_PAPER_PRIVATE_KEY_PATH", "").strip()
            or os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        )
        return key_id, key_secret, key_path

    if key_profile == "real":
        key_id = (
            os.getenv("KALSHI_REAL_API_KEY_ID", "").strip()
            or os.getenv("KALSHI_API_KEY_ID", "").strip()
        )
        key_secret = (
            os.getenv("KALSHI_REAL_API_KEY_SECRET", "").strip()
            or os.getenv("KALSHI_API_KEY_SECRET", "").strip()
        )
        key_path = (
            os.getenv("KALSHI_REAL_PRIVATE_KEY_PATH", "").strip()
            or os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        )
        return key_id, key_secret, key_path

    return (
        os.getenv("KALSHI_API_KEY_ID", "").strip(),
        os.getenv("KALSHI_API_KEY_SECRET", "").strip(),
        os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip(),
    )


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
    bot_mode: str
    kalshi_key_profile: str
    poll_interval_seconds: int
    market_limit: int
    historical_days: int
    historical_markets: int
    run_historical_backfill_on_start: bool
    kalshi_stub_mode: bool
    kalshi_base_url: str
    kalshi_use_auth_for_public_data: bool
    websocket_enabled: bool
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
    trading_profile: str
    paper_trading_enabled: bool
    paper_trading_mode: str
    paper_trading_base_url: str
    paper_trade_signal_types: list[str]
    paper_trade_min_edge_bps: int
    paper_trade_min_confidence: float
    paper_trade_contract_count: int
    paper_trade_max_orders_per_cycle: int
    paper_trade_cooldown_minutes: int
    paper_trade_min_price_cents: int
    paper_trade_max_price_cents: int
    paper_trade_maker_only: bool
    paper_trade_enable_arbitrage: bool
    paper_trade_enable_queue_management: bool
    paper_trade_queue_max_depth: int
    paper_trade_queue_stale_minutes: int
    paper_trade_reprice_cooldown_minutes: int
    paper_trade_sizing_mode: str
    kelly_fraction_scale: float
    paper_trade_max_position_dollars: float
    paper_trade_max_portfolio_exposure_dollars: float
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_notify_actionable_only: bool
    telegram_notify_execution_events: bool
    telegram_min_edge_bps: int
    edge_decay_alert_threshold_bps: int
    signal_min_edge_bps: int
    signal_store_all: bool

    @classmethod
    def from_env(cls) -> "Settings":
        database_url, database_url_source = resolve_database_url()
        bot_mode = _as_bot_mode(os.getenv("BOT_MODE"), "custom")
        mode_defaults = BOT_MODE_DEFAULTS.get(bot_mode, {})

        default_key_profile = str(mode_defaults.get("kalshi_key_profile", "direct"))
        kalshi_key_profile = _as_key_profile(
            os.getenv("KALSHI_KEY_PROFILE"), default_key_profile
        )
        kalshi_api_key_id, kalshi_api_key_secret, kalshi_private_key_path = (
            _resolve_kalshi_credentials(kalshi_key_profile)
        )

        default_trading_profile = str(mode_defaults.get("trading_profile", "balanced"))
        trading_profile = _as_trading_profile(
            os.getenv("TRADING_PROFILE"), default_trading_profile
        )
        profile_defaults = TRADING_PROFILE_DEFAULTS[trading_profile]
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

        paper_trade_signal_types = _as_signal_types(
            os.getenv("PAPER_TRADE_SIGNAL_TYPES"), ["weather", "btc"]
        )
        if not paper_trade_signal_types:
            paper_trade_signal_types = ["weather", "btc"]

        paper_trade_contract_count = _as_int(
            os.getenv("PAPER_TRADE_CONTRACT_COUNT"),
            int(profile_defaults["paper_trade_contract_count"]),
        )
        if paper_trade_contract_count < 1:
            paper_trade_contract_count = 1
        if paper_trade_contract_count > 50:
            paper_trade_contract_count = 50

        paper_trade_max_orders_per_cycle = _as_int(
            os.getenv("PAPER_TRADE_MAX_ORDERS_PER_CYCLE"),
            int(profile_defaults["paper_trade_max_orders_per_cycle"]),
        )
        if paper_trade_max_orders_per_cycle < 1:
            paper_trade_max_orders_per_cycle = 1
        if paper_trade_max_orders_per_cycle > 20:
            paper_trade_max_orders_per_cycle = 20

        paper_trade_cooldown_minutes = _as_int(
            os.getenv("PAPER_TRADE_COOLDOWN_MINUTES"),
            int(profile_defaults["paper_trade_cooldown_minutes"]),
        )
        if paper_trade_cooldown_minutes < 1:
            paper_trade_cooldown_minutes = 1

        paper_trade_min_price_cents = _as_int(
            os.getenv("PAPER_TRADE_MIN_PRICE_CENTS"),
            int(profile_defaults["paper_trade_min_price_cents"]),
        )
        paper_trade_max_price_cents = _as_int(
            os.getenv("PAPER_TRADE_MAX_PRICE_CENTS"),
            int(profile_defaults["paper_trade_max_price_cents"]),
        )
        if paper_trade_min_price_cents < 1:
            paper_trade_min_price_cents = 1
        if paper_trade_max_price_cents > 99:
            paper_trade_max_price_cents = 99
        if paper_trade_min_price_cents > paper_trade_max_price_cents:
            paper_trade_min_price_cents = min(paper_trade_max_price_cents, 5)

        paper_trade_queue_max_depth = _as_int(
            os.getenv("PAPER_TRADE_QUEUE_MAX_DEPTH"), 50
        )
        if paper_trade_queue_max_depth < 1:
            paper_trade_queue_max_depth = 1

        paper_trade_queue_stale_minutes = _as_int(
            os.getenv("PAPER_TRADE_QUEUE_STALE_MINUTES"), 10
        )
        if paper_trade_queue_stale_minutes < 1:
            paper_trade_queue_stale_minutes = 1

        paper_trade_reprice_cooldown_minutes = _as_int(
            os.getenv("PAPER_TRADE_REPRICE_COOLDOWN_MINUTES"), 20
        )
        if paper_trade_reprice_cooldown_minutes < 1:
            paper_trade_reprice_cooldown_minutes = 1

        paper_trade_min_confidence = _as_float(
            os.getenv("PAPER_TRADE_MIN_CONFIDENCE"),
            float(profile_defaults["paper_trade_min_confidence"]),
        )
        if paper_trade_min_confidence < 0.0:
            paper_trade_min_confidence = 0.0
        if paper_trade_min_confidence > 1.0:
            paper_trade_min_confidence = 1.0

        kelly_fraction_scale = _as_float(os.getenv("KELLY_FRACTION_SCALE"), 0.25)
        if kelly_fraction_scale < 0.0:
            kelly_fraction_scale = 0.0
        if kelly_fraction_scale > 1.0:
            kelly_fraction_scale = 1.0

        max_position_dollars = _as_float(
            os.getenv("PAPER_TRADE_MAX_POSITION_DOLLARS"), 50.0
        )
        if max_position_dollars < 1.0:
            max_position_dollars = 1.0

        max_portfolio_exposure_dollars = _as_float(
            os.getenv("PAPER_TRADE_MAX_PORTFOLIO_EXPOSURE_DOLLARS"), 500.0
        )
        if max_portfolio_exposure_dollars < max_position_dollars:
            max_portfolio_exposure_dollars = max_position_dollars

        return cls(
            database_url=database_url,
            database_url_source=database_url_source,
            bot_mode=bot_mode,
            kalshi_key_profile=kalshi_key_profile,
            poll_interval_seconds=_as_int(os.getenv("POLL_INTERVAL_SECONDS"), 300),
            market_limit=_as_int(os.getenv("MARKET_LIMIT"), 25),
            historical_days=_as_int(os.getenv("HISTORICAL_DAYS"), 7),
            historical_markets=_as_int(os.getenv("HISTORICAL_MARKETS"), 10),
            run_historical_backfill_on_start=_as_bool(
                os.getenv("RUN_HISTORICAL_BACKFILL_ON_START"), True
            ),
            kalshi_stub_mode=_as_bool(
                os.getenv("KALSHI_STUB_MODE"),
                bool(mode_defaults.get("kalshi_stub_mode", True)),
            ),
            kalshi_base_url=_normalize_kalshi_base_url(
                os.getenv(
                    "KALSHI_BASE_URL",
                    str(mode_defaults.get("kalshi_base_url", "https://api.elections.kalshi.com")),
                )
            ),
            kalshi_use_auth_for_public_data=_as_bool(
                os.getenv("KALSHI_USE_AUTH_FOR_PUBLIC_DATA"), False
            ),
            websocket_enabled=_as_bool(os.getenv("WEBSOCKET_ENABLED"), False),
            kalshi_api_key_id=kalshi_api_key_id,
            kalshi_api_key_secret=kalshi_api_key_secret,
            kalshi_private_key_path=kalshi_private_key_path,
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
            trading_profile=trading_profile,
            paper_trading_enabled=_as_bool(
                os.getenv("PAPER_TRADING_ENABLED"),
                bool(mode_defaults.get("paper_trading_enabled", False)),
            ),
            paper_trading_mode=_as_paper_trading_mode(
                os.getenv(
                    "PAPER_TRADING_MODE",
                    str(mode_defaults.get("paper_trading_mode", "simulate")),
                )
            ),
            paper_trading_base_url=_normalize_kalshi_base_url(
                os.getenv(
                    "PAPER_TRADING_BASE_URL",
                    str(mode_defaults.get("paper_trading_base_url", "https://demo-api.kalshi.co")),
                )
            ),
            paper_trade_signal_types=paper_trade_signal_types,
            paper_trade_min_edge_bps=_as_int(
                os.getenv("PAPER_TRADE_MIN_EDGE_BPS"),
                int(profile_defaults["paper_trade_min_edge_bps"]),
            ),
            paper_trade_min_confidence=paper_trade_min_confidence,
            paper_trade_contract_count=paper_trade_contract_count,
            paper_trade_max_orders_per_cycle=paper_trade_max_orders_per_cycle,
            paper_trade_cooldown_minutes=paper_trade_cooldown_minutes,
            paper_trade_min_price_cents=paper_trade_min_price_cents,
            paper_trade_max_price_cents=paper_trade_max_price_cents,
            paper_trade_maker_only=_as_bool(os.getenv("PAPER_TRADE_MAKER_ONLY"), True),
            paper_trade_enable_arbitrage=_as_bool(
                os.getenv("PAPER_TRADE_ENABLE_ARBITRAGE"), True
            ),
            paper_trade_enable_queue_management=_as_bool(
                os.getenv("PAPER_TRADE_ENABLE_QUEUE_MANAGEMENT"), True
            ),
            paper_trade_queue_max_depth=paper_trade_queue_max_depth,
            paper_trade_queue_stale_minutes=paper_trade_queue_stale_minutes,
            paper_trade_reprice_cooldown_minutes=paper_trade_reprice_cooldown_minutes,
            paper_trade_sizing_mode=_as_paper_trade_sizing_mode(
                os.getenv("PAPER_TRADE_SIZING_MODE")
            ),
            kelly_fraction_scale=kelly_fraction_scale,
            paper_trade_max_position_dollars=max_position_dollars,
            paper_trade_max_portfolio_exposure_dollars=max_portfolio_exposure_dollars,
            telegram_enabled=_as_bool(os.getenv("TELEGRAM_ENABLED"), False),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            telegram_notify_actionable_only=_as_bool(
                os.getenv("TELEGRAM_NOTIFY_ACTIONABLE_ONLY"), True
            ),
            telegram_notify_execution_events=_as_bool(
                os.getenv("TELEGRAM_NOTIFY_EXECUTION_EVENTS"), True
            ),
            telegram_min_edge_bps=_as_int(
                os.getenv("TELEGRAM_MIN_EDGE_BPS"),
                int(profile_defaults["telegram_min_edge_bps"]),
            ),
            edge_decay_alert_threshold_bps=_as_int(
                os.getenv("EDGE_DECAY_ALERT_THRESHOLD_BPS"), 75
            ),
            signal_min_edge_bps=_as_int(
                os.getenv("SIGNAL_MIN_EDGE_BPS"),
                int(profile_defaults["signal_min_edge_bps"]),
            ),
            signal_store_all=_as_bool(os.getenv("SIGNAL_STORE_ALL"), True),
        )
