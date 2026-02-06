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
    kalshi_api_key_id: str
    kalshi_api_key_secret: str

    @classmethod
    def from_env(cls) -> "Settings":
        database_url, database_url_source = resolve_database_url()
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
            kalshi_base_url=os.getenv("KALSHI_BASE_URL", "https://api.kalshi.com"),
            kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID", ""),
            kalshi_api_key_secret=os.getenv("KALSHI_API_KEY_SECRET", ""),
        )
