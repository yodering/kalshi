# Kalshi Week 1 Pipeline

Minimal Week 1 data pipeline for Kalshi market ingestion with:

- Kalshi client wiring with signed live requests
- Market and snapshot schema in PostgreSQL
- Open-Meteo ensemble ingestion for NYC weather
- BTC spot ingestion from configurable sources (Coinbase/Kraken/Bitstamp core; Binance optional)
- Weather/BTC signal generation + optional auto paper execution
- Optional Telegram notifications
- Optional auto paper-trade execution (simulation or Kalshi demo endpoint)
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

Run async polling + websocket feeds (Kalshi, Binance, Coinbase, Kraken):

```bash
python3 -m kalshi_pipeline.main run-async
```

Operational/debug CLI:

```bash
python3 -m kalshi_pipeline.cli status
python3 -m kalshi_pipeline.cli positions
python3 -m kalshi_pipeline.cli accuracy --days 30
python3 -m kalshi_pipeline.cli signals --last 20
python3 -m kalshi_pipeline.cli trades --last 20
python3 -m kalshi_pipeline.cli orderbook KXBTC15M-26FEB081830-30
python3 -m kalshi_pipeline.cli forecast --last 40
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
- `BOT_MODE`: `custom`, `demo_safe`, `live_safe`, or `live_auto`
- `KALSHI_STUB_MODE`: `true` or `false` (default `false` in `.env.example`)
- `KALSHI_BASE_URL`: Kalshi base URL (default `https://api.elections.kalshi.com`)
  - Recommended production host: `https://api.elections.kalshi.com`
  - Recommended demo host: `https://demo-api.kalshi.co`
- `KALSHI_USE_AUTH_FOR_PUBLIC_DATA`: sign read-only requests too (default `false`)
- `WEBSOCKET_ENABLED`: enable async runtime with websocket feeds when using `run`/`run-async`
- `KALSHI_KEY_PROFILE`: `direct`, `paper`, or `real`
- `KALSHI_API_KEY_ID`: Kalshi key id
- `KALSHI_API_KEY_SECRET`: private key PEM contents (or a file path if no PEM header is present)
- `KALSHI_PRIVATE_KEY_PATH`: optional explicit path to key PEM file
- `KALSHI_PRIVATE_KEY_PASSWORD`: optional private key password
- `KALSHI_PAPER_API_KEY_ID`: demo key id used when `KALSHI_KEY_PROFILE=paper`
- `KALSHI_PAPER_API_KEY_SECRET`: demo private key PEM used when `KALSHI_KEY_PROFILE=paper`
- `KALSHI_PAPER_PRIVATE_KEY_PATH`: optional demo key path
- `KALSHI_REAL_API_KEY_ID`: production key id used when `KALSHI_KEY_PROFILE=real`
- `KALSHI_REAL_API_KEY_SECRET`: production private key PEM used when `KALSHI_KEY_PROFILE=real`
- `KALSHI_REAL_PRIVATE_KEY_PATH`: optional production key path
- `TARGET_MARKET_QUERY_GROUPS`: semicolon-separated keyword groups (default: highest NYC temp today + BTC up/down 15m)
- `TARGET_MARKET_STATUS`: market status filter (default `open`, set `any` to disable status filter)
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
- `BTC_ENABLED_SOURCES`: comma-separated BTC price sources to query (default `coinbase,kraken,bitstamp`)
- `BTC_CORE_SOURCES`: comma-separated core sources used for fair-value composite (default `coinbase,kraken,bitstamp`)
- `BTC_MIN_CORE_SOURCES`: minimum core sources required to emit BTC signals (default `2`)
- `BTC_MOMENTUM_LOOKBACK_MINUTES`: lookback used in BTC momentum signal
- `TRADING_PROFILE`: `conservative`, `balanced`, or `aggressive` preset for signal/trade thresholds
- `PAPER_TRADING_ENABLED`: enable auto paper execution (`false` by default)
- `PAPER_TRADING_MODE`: `simulate` or `kalshi_demo`
- `PAPER_TRADING_BASE_URL`: paper trading API host (default `https://demo-api.kalshi.co`)
- `PAPER_TRADE_SIGNAL_TYPES`: which signal types are tradable (`weather,btc` by default)
- `PAPER_TRADE_MIN_EDGE_BPS`: minimum edge to place an auto paper order
- `PAPER_TRADE_MIN_CONFIDENCE`: minimum confidence to place an auto paper order
- `PAPER_TRADE_CONTRACT_COUNT`: fixed contracts per paper order
- `PAPER_TRADE_MAX_ORDERS_PER_CYCLE`: cap paper orders each poll cycle
- `PAPER_TRADE_COOLDOWN_MINUTES`: cooldown before repeating same ticker+direction
- `PAPER_TRADE_MIN_PRICE_CENTS`: lower clamp for limit order price
- `PAPER_TRADE_MAX_PRICE_CENTS`: upper clamp for limit order price
- `PAPER_TRADE_MAKER_ONLY`: force maker-style pricing on auto orders
- `PAPER_TRADE_ENABLE_ARBITRAGE`: place paired yes/no orders when `yes_ask + no_ask < 100`
- `PAPER_TRADE_SIZING_MODE`: `fixed` or `kelly`
- `KELLY_FRACTION_SCALE`: Kelly multiplier (`0.25` default for quarter-Kelly)
- `PAPER_TRADE_MAX_POSITION_DOLLARS`: hard cap per order
- `PAPER_TRADE_MAX_PORTFOLIO_EXPOSURE_DOLLARS`: hard cap across open exposure
- `TELEGRAM_ENABLED`: enable Telegram notifications
- `TELEGRAM_BOT_TOKEN`: Telegram bot token
- `TELEGRAM_CHAT_ID`: Telegram chat id
- `TELEGRAM_NOTIFY_ACTIONABLE_ONLY`: if true, only actionable signal digest messages
- `TELEGRAM_NOTIFY_EXECUTION_EVENTS`: if true, send paper execution digest messages
- `TELEGRAM_MIN_EDGE_BPS`: minimum edge threshold for actionable Telegram digest filtering
- `EDGE_DECAY_ALERT_THRESHOLD_BPS`: alert when open position edge decays below threshold
- `SIGNAL_MIN_EDGE_BPS`: minimum edge for actionable direction
- `SIGNAL_STORE_ALL`: store flat signals too (`true` by default)

`TRADING_PROFILE` sets defaults for:
- `PAPER_TRADE_MIN_EDGE_BPS`
- `PAPER_TRADE_MIN_CONFIDENCE`
- `PAPER_TRADE_CONTRACT_COUNT`
- `PAPER_TRADE_MAX_ORDERS_PER_CYCLE`
- `PAPER_TRADE_COOLDOWN_MINUTES`
- `PAPER_TRADE_MIN_PRICE_CENTS`
- `PAPER_TRADE_MAX_PRICE_CENTS`
- `TELEGRAM_MIN_EDGE_BPS`
- `SIGNAL_MIN_EDGE_BPS`

Explicit env values still win over the profile defaults.

`BOT_MODE` sets defaults for URLs, key profile, and execution behavior:
- `demo_safe`: demo URLs, paper key profile, conservative profile, auto paper trading enabled
- `live_safe`: production URLs, real key profile, conservative profile, auto trading disabled
- `live_auto`: production URLs, real key profile, conservative profile, auto trading enabled
- `custom`: no opinionated mode defaults

For a simple Railway setup, keep only these vars first and rely on defaults for the rest:
- `DATABASE_URL`
- `BOT_MODE=demo_safe`
- `KALSHI_KEY_PROFILE=paper`
- `KALSHI_PAPER_API_KEY_ID`
- `KALSHI_PAPER_API_KEY_SECRET`
- `TELEGRAM_ENABLED=true`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

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
- Auto paper execution is disabled by default. Enable with:
  - `PAPER_TRADING_ENABLED=true`
  - `PAPER_TRADING_MODE=kalshi_demo` to place demo orders automatically
  - `PAPER_TRADING_MODE=simulate` to record "would trade" intents only
- For `kalshi_demo` mode, keep data and order environments aligned to avoid ticker mismatches
  between production and demo market catalogs.
- Telegram command interface (same configured chat id):
  - `/status`, `/signals`, `/orders`, `/positions`, `/balance`, `/accuracy [days]`
  - `/pause`, `/resume`
  - `/mode [custom|demo_safe|live_safe|live_auto]`
  - `CONFIRM LIVE` to confirm pending live mode change

## 6. Stored Tables

- `markets`
- `market_snapshots`
- `weather_ensemble_samples`
- `crypto_spot_ticks`
- `signals`
- `paper_trade_orders`
- `alert_events`
