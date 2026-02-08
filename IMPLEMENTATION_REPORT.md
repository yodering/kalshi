# Kalshi Bot Implementation Report

## 1) Project Scope and Outcomes

This project started as Week 1 pipeline work (data collection + storage) and was extended into a working signal and execution system for two Kalshi market families:

- NYC highest temperature markets (`KXHIGHNY`)
- BTC 15-minute up/down markets (`KXBTC15M`)

Current result:

- Continuous polling worker is running on Railway
- Market/snapshot data is stored in PostgreSQL
- Weather and BTC signals are generated and persisted
- Telegram digests are being sent
- Demo (paper) auto-orders are being submitted successfully

## 2) What Is Implemented

### 2.1 Data Ingestion and Market Discovery

- Kalshi REST integration via `kalshi_pipeline/kalshi_client.py`
- Public-data collection without auth by default
- Optional auth signing for private endpoints (orders, portfolio)
- Target discovery by:
  - series (`TARGET_SERIES_TICKERS`)
  - keyword groups (`TARGET_MARKET_QUERY_GROUPS`)
  - explicit tickers/events if set
- Live contract auto-selection:
  - Weather: selects one active event and all its brackets
  - BTC: selects nearest active 15-minute contract

### 2.2 Storage Model (Postgres)

Schema in `kalshi_pipeline/schema.sql` includes:

- `markets`
- `market_snapshots`
- `weather_ensemble_samples`
- `crypto_spot_ticks`
- `signals`
- `paper_trade_orders`
- `alert_events`

The worker ensures schema exists at startup (`ensure_schema()`), so manual table creation is not required.

### 2.3 Signal Generation

- Weather model in `kalshi_pipeline/signals/weather.py`
  - Parses weather bracket bounds from market metadata/title/subtitle
  - Computes implied probabilities from ensemble max-temp outcomes
  - Compares model probability vs market probability to compute `edge_bps`
- BTC model in `kalshi_pipeline/signals/btc.py`
  - Builds a fair value probability from multi-source median spot and momentum
  - Uses core-source quorum (`BTC_CORE_SOURCES`, `BTC_MIN_CORE_SOURCES`)
  - Produces actionable `buy_yes` / `buy_no` directions when edge passes threshold

### 2.4 Paper Trading Engine

Implemented in `kalshi_pipeline/paper_trading.py`:

- Filters actionable signals by:
  - signal type allowlist
  - minimum edge
  - minimum confidence
- Applies cooldown by ticker+direction
- Applies per-cycle order cap
- Converts snapshot prices to cent limits with min/max clamps
- Supports two modes:
  - `simulate` (record only)
  - `kalshi_demo` (submit order to Kalshi endpoint)
- Records execution attempts and outcomes in `paper_trade_orders`

### 2.5 Telegram Notifications

Implemented in `kalshi_pipeline/notifications.py`:

- Signal digest messages
- Paper execution digest messages
- Actionable-only filtering option
- Failure reason inclusion for failed order attempts
- Emoji-enhanced formatting for readability
- Notification persistence in `alert_events`

### 2.6 Configuration and Mode Management

Implemented in `kalshi_pipeline/config.py`:

- `TRADING_PROFILE` presets:
  - `conservative`
  - `balanced`
  - `aggressive`
- `BOT_MODE` presets:
  - `demo_safe`
  - `live_safe`
  - `live_auto`
  - `custom`
- Key-profile selection:
  - `KALSHI_KEY_PROFILE=paper|real|direct`
- Separate key bundles for demo and real:
  - `KALSHI_PAPER_API_KEY_ID` / `KALSHI_PAPER_API_KEY_SECRET`
  - `KALSHI_REAL_API_KEY_ID` / `KALSHI_REAL_API_KEY_SECRET`

This allows environment switching with minimal variable edits.

## 3) Verified Working Status

Based on production logs and DB checks:

- Pipeline polls successfully (`poll_complete`)
- Markets discovered and snapshots inserted
- Signals generated and stored
- Telegram alerts are sent (`alert_events.status='sent'`)
- Demo order submission confirmed:
  - `paper_trade_orders.status='submitted'`
  - `external_order_id` populated for submitted orders

Example observed in database:

- Status counts: `failed=6`, `submitted=1`
- Most recent order: `submitted` with external order id present
- Earlier failures were tied to invalid private key serialization and resolved after key fix

## 4) Methodology and Decision Rationale

### 4.1 Reliability-First Pipeline Design

- Non-critical data-source failures do not crash the worker
- Collector failures are logged and the cycle continues
- `poll_failed` is reserved for top-level cycle exceptions
- This keeps the bot operational even when one provider is degraded

### 4.2 Graceful Degradation for External APIs

- Weather collector attempts ensemble endpoint first, then forecast fallback
- BTC collector supports multiple exchanges and continues with partial availability
- Historical candlestick endpoint 404s are treated as unavailable data, not fatal

### 4.3 Deterministic Risk Controls

- Hard caps on orders per cycle, cooldown windows, and price clamps
- Signal threshold gating (`edge_bps`, confidence)
- Default-safe profiles (`conservative`) for real-capital protection

### 4.4 Environment Parity and Safety

- Data and execution URLs can be aligned through mode defaults
- Key-profile separation prevents accidental credential crossover
- `live_safe` mode explicitly disables auto-trading by default

### 4.5 Observability for Debuggability

- Structured poll metrics in logs
- Persistent audit tables for:
  - signals
  - order attempts/results
  - notification events
- Failure reasons are stored and forwarded to Telegram for quick diagnosis

## 5) Current Operational Recommendations

- Preferred daily mode for testing: `BOT_MODE=demo_safe`
- Keep real and paper keys both configured, switch via:
  - `KALSHI_KEY_PROFILE=paper` for demo
  - `KALSHI_KEY_PROFILE=real` for production
- Keep `TRADING_PROFILE=conservative` until live behavior is statistically validated

## 6) Known Limitations

- Kalshi candlestick endpoints are not consistently available for all contracts
- Open-Meteo ensemble endpoint may 404 depending on model parameters or availability
- Exchange coverage can vary by region/provider policy
- Telegram mode switching via chat command is not implemented yet

## 7) Suggested Next Engineering Steps

- Add read-only admin command interface for bot state (`/status`, `/positions`, `/mode`)
- Add explicit mode-change confirmation flow before enabling live auto execution
- Add unit tests for:
  - profile/mode resolution
  - signal gating
  - order price clamp logic
- Add a compact `debug-stats` CLI command to avoid ad hoc SQL for common checks

## 8) Key Files

- `kalshi_pipeline/main.py`
- `kalshi_pipeline/config.py`
- `kalshi_pipeline/pipeline.py`
- `kalshi_pipeline/kalshi_client.py`
- `kalshi_pipeline/collectors/weather.py`
- `kalshi_pipeline/collectors/crypto.py`
- `kalshi_pipeline/signals/weather.py`
- `kalshi_pipeline/signals/btc.py`
- `kalshi_pipeline/paper_trading.py`
- `kalshi_pipeline/notifications.py`
- `kalshi_pipeline/db.py`
- `kalshi_pipeline/schema.sql`
- `README.md`
