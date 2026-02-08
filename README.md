# Kalshi Week 1 Pipeline

Minimal Week 1 data pipeline for Kalshi market ingestion with:

- Kalshi client wiring with signed live requests
- Market and snapshot schema in PostgreSQL
- Open-Meteo ensemble ingestion for NYC weather
- BTC spot ingestion from Binance, Coinbase, and Kraken
- Weather/BTC signal scaffolding (no trade execution yet)
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

Preview matched target markets:

```bash
python3 -m kalshi_pipeline.main discover-targets
```

## 3. Environment Variables

- `DATABASE_URL`: PostgreSQL connection string
- `POLL_INTERVAL_SECONDS`: polling interval (default `300`)
- `MARKET_LIMIT`: number of markets per cycle (default `25`)
- `HISTORICAL_DAYS`: backfill window on startup (default `7`)
- `HISTORICAL_MARKETS`: number of markets to backfill (default `10`)
- `RUN_HISTORICAL_BACKFILL_ON_START`: `true` or `false`
- `KALSHI_STUB_MODE`: `true` or `false` (default `false` in `.env.example`)
- `KALSHI_BASE_URL`: Kalshi base URL (default `https://api.elections.kalshi.com`)
- `KALSHI_USE_AUTH_FOR_PUBLIC_DATA`: sign read-only requests too (default `false`)
- `KALSHI_API_KEY_ID`: Kalshi key id
- `KALSHI_API_KEY_SECRET`: private key PEM contents (or a file path if no PEM header is present)
- `KALSHI_PRIVATE_KEY_PATH`: optional explicit path to key PEM file
- `KALSHI_PRIVATE_KEY_PASSWORD`: optional private key password
- `TARGET_MARKET_QUERY_GROUPS`: semicolon-separated keyword groups (default: highest NYC temp today + BTC up/down 15m)
- `TARGET_MARKET_STATUS`: market status filter (default `open`)
- `TARGET_MARKET_DISCOVERY_PAGES`: max pages scanned during target discovery (default `10`)
- `TARGET_MARKET_TICKERS`: optional comma-separated exact market tickers (or full Kalshi market URLs)
- `TARGET_EVENT_TICKERS`: optional comma-separated exact event tickers
- `TARGET_SERIES_TICKERS`: comma-separated series (default `KXHIGHNY,KXBTC15M`)
- `AUTO_SELECT_LIVE_CONTRACTS`: auto-pick current contracts when tickers are not pinned
- `STORE_RAW_JSON`: whether to persist full raw API payloads (default `false`)
- `WEATHER_ENABLED`: enable Open-Meteo ensemble collector
- `WEATHER_LATITUDE`: forecast latitude (default Central Park)
- `WEATHER_LONGITUDE`: forecast longitude (default Central Park)
- `WEATHER_TIMEZONE`: timezone used for "today" weather target date
- `WEATHER_ENSEMBLE_MODELS`: comma-separated Open-Meteo ensemble models
- `WEATHER_FORECAST_DAYS`: how far ahead to request weather data
- `BTC_ENABLED`: enable BTC spot collectors
- `BTC_SYMBOL`: symbol label stored in DB (`BTCUSD` by default)
- `BTC_MOMENTUM_LOOKBACK_MINUTES`: lookback used in BTC momentum signal
- `SIGNAL_MIN_EDGE_BPS`: minimum edge for actionable direction
- `SIGNAL_STORE_ALL`: store flat signals too (`true` by default)

## 4. Railway Deployment

1. Create a Railway project and link this repo.
2. Add a PostgreSQL service.
3. Add env vars:
- `DATABASE_URL` exactly as a reference to Postgres service value, no quotes:
  - `${{Postgres.DATABASE_URL}}`
- `KALSHI_STUB_MODE=false`
- Optional tuning vars (`POLL_INTERVAL_SECONDS`, `MARKET_LIMIT`, etc.)
4. Deploy the service (uses `railway.json` + `Dockerfile`).
5. Confirm logs show `poll_complete` every interval.

## 5.1 Railway DB Troubleshooting

If logs show `Name or service not known` with host `postgres.railway.internal`, your worker cannot resolve Railway private DNS in its current runtime/environment.

Use one of these fixes:

1. Preferred quick fix:
- Set `DATABASE_URL=${{Postgres.DATABASE_PUBLIC_URL}}`
- The app will enforce `sslmode=require` for public Railway hosts when missing.

2. Explicit PG vars:
- `PGHOST=${{Postgres.PGHOST}}`
- `PGPORT=${{Postgres.PGPORT}}`
- `PGUSER=${{Postgres.PGUSER}}`
- `PGPASSWORD=${{Postgres.PGPASSWORD}}`
- `PGDATABASE=${{Postgres.PGDATABASE}}`

After any variable change, redeploy the worker service.

## 5. Notes

- Live mode now signs each request (`timestamp + method + path`) using RSA-PSS/SHA256.
- Public read endpoints (markets/orderbook/trades) run without auth by default for lower complexity and fewer failure points.
- If no markets match your filters, logs will show:
  - `No markets matched current target filters. Check TARGET_* env settings.`
- When `TARGET_MARKET_TICKERS` is set, the pipeline fetches those exact contracts directly.
- With `AUTO_SELECT_LIVE_CONTRACTS=true` and no pinned tickers:
  - KXHIGHNY: selects one live event and ingests all brackets
  - KXBTC15M: selects nearest live 15-minute contract
- This build stores signals only; no order placement endpoints are called yet.

## 6. Stored Tables

- `markets`
- `market_snapshots`
- `weather_ensemble_samples`
- `crypto_spot_ticks`
- `signals`
