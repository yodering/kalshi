# Kalshi Week 1 Pipeline

Minimal Week 1 data pipeline for Kalshi market ingestion with:

- Kalshi client wiring (stub-first, live mode placeholders)
- Market and snapshot schema in PostgreSQL
- Historical backfill on startup
- Polling loop with persistent storage
- Railway-ready worker deployment

## 1. Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set env vars from `.env` before running (or export manually).

## 2. Commands

Initialize schema:

```bash
python3 -m kalshi_pipeline.main init-db
```

Run one collection cycle:

```bash
python3 -m kalshi_pipeline.main run-once
```

Run continuous polling:

```bash
python3 -m kalshi_pipeline.main run
```

Check API connectivity mode:

```bash
python3 -m kalshi_pipeline.main health-check
```

## 3. Environment Variables

- `DATABASE_URL`: PostgreSQL connection string
- `POLL_INTERVAL_SECONDS`: polling interval (default `300`)
- `MARKET_LIMIT`: number of markets per cycle (default `25`)
- `HISTORICAL_DAYS`: backfill window on startup (default `7`)
- `HISTORICAL_MARKETS`: number of markets to backfill (default `10`)
- `RUN_HISTORICAL_BACKFILL_ON_START`: `true` or `false`
- `KALSHI_STUB_MODE`: `true` or `false` (default `true`)
- `KALSHI_BASE_URL`: Kalshi base URL
- `KALSHI_API_KEY_ID`: used for live mode placeholder auth headers
- `KALSHI_API_KEY_SECRET`: used for live mode placeholder auth headers

## 4. Railway Deployment

1. Create a Railway project and link this repo.
2. Add a PostgreSQL service.
3. Add env vars:
- `DATABASE_URL` exactly as a reference to Postgres service value, no quotes:
  - `${{Postgres.DATABASE_URL}}`
- `KALSHI_STUB_MODE=true` for now
- Optional tuning vars (`POLL_INTERVAL_SECONDS`, `MARKET_LIMIT`, etc.)
4. Deploy the service (uses `railway.json` + `Dockerfile`).
5. Confirm logs show `poll_complete` every interval.

## 5. Notes

- Current auth in live mode is intentionally a placeholder for Week 1.
- Replace `KalshiClient._build_auth_headers` with the official Kalshi signing flow once credentials are available.
