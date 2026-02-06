from __future__ import annotations

from dataclasses import dataclass
import os


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    database_url: str
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
        return cls(
            database_url=os.getenv(
                "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/kalshi"
            ),
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

