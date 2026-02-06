from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random

from .models import Market, MarketSnapshot


def _seeded_random(seed: str) -> random.Random:
    return random.Random(seed)


def generate_markets(limit: int) -> list[Market]:
    now = datetime.now(timezone.utc)
    base = [
        ("KXFEDRATE-DECISION", "Will the Fed cut rates at next FOMC?"),
        ("KELECT-PRES-2028", "Will Democrats win the 2028 US presidential election?"),
        ("KWEATHER-NYC-SNOW", "Will NYC get at least 2 inches of snow this week?"),
        ("KECON-CPI-UP", "Will CPI print above consensus this month?"),
        ("KSPORTS-SB-WINNER", "Will Team A win the championship game?"),
    ]
    markets: list[Market] = []
    for idx in range(min(limit, len(base))):
        ticker, title = base[idx]
        markets.append(
            Market(
                ticker=ticker,
                title=title,
                status="open",
                close_time=now + timedelta(days=30 + (idx * 10)),
                raw_json={"source": "stub", "rank": idx + 1},
            )
        )
    return markets


def generate_current_snapshot(market: Market, at_time: datetime) -> MarketSnapshot:
    rng = _seeded_random(f"{market.ticker}:{at_time.replace(second=0, microsecond=0).isoformat()}")
    yes_price = round(rng.uniform(0.1, 0.9), 3)
    no_price = round(1 - yes_price, 3)
    volume = round(rng.uniform(500, 15000), 2)
    return MarketSnapshot(
        ticker=market.ticker,
        ts=at_time,
        yes_price=yes_price,
        no_price=no_price,
        volume=volume,
        raw_json={"source": "stub"},
    )


def generate_historical_snapshots(
    market: Market, start: datetime, end: datetime, step_minutes: int = 60
) -> list[MarketSnapshot]:
    snapshots: list[MarketSnapshot] = []
    current = start
    while current <= end:
        snapshots.append(generate_current_snapshot(market, current))
        current += timedelta(minutes=step_minutes)
    return snapshots

