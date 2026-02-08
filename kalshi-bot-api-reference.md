# Kalshi Prediction Market Bot — API & Data Source Reference

## Two Target Markets

| Property | NYC High Temperature (KXHIGHNY) | BTC Up or Down 15-Min (KXBTC15M) |
|---|---|---|
| **Ticker prefix** | `KXHIGHNY` | `KXBTC15M` |
| **Structure** | 6 bracket contracts per day (e.g., "below 30°F", "30–31°F", "32–33°F", "34–35°F", "36–37°F", "38°F+") | Binary YES/NO — will BTC be higher or lower after 15 minutes? |
| **Resolution frequency** | 1/day (next morning) | ~96/day (every 15 min, 24/7) |
| **Resolution source** | NWS Daily Climate Report for Central Park (KNYC station) | CF Benchmarks Real-Time Index (BRTI) — 1-min window of per-second observations, trimmed average (top/bottom 20% excluded) |
| **Edge type** | Informational — weather model divergence vs. market consensus | Structural — maker spread capture, arbitrage when YES+NO < $1, price feed latency |
| **Volume** | Moderate, retail-heavy | ~$35–55K per cycle, bot-heavy |
| **Maker fee** | $0 | $0 |
| **Taker fee** | $0.07 × P × (1-P) per contract, rounded up | Same formula |

---

## 1. KALSHI API

The single most important data source — provides market data, orderbooks, historical trades, and trade execution.

### Base URLs

```
Production REST:   https://api.elections.kalshi.com/trade-api/v2
Production WS:     wss://api.elections.kalshi.com/trade-api/ws/v2
Demo REST:         https://demo-api.kalshi.co/trade-api/v2
Demo WS:           wss://demo-api.kalshi.co/trade-api/ws/v2
```

Note: Despite the "elections" subdomain, this serves ALL Kalshi markets.

### Authentication

- API keys generated in Settings → API on Kalshi dashboard
- Request signing: RSA-PSS with SHA256
- Signature payload: `{timestamp_ms}{METHOD}{path_without_query_params}`
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`
- Public endpoints (markets, orderbooks, trades) require NO auth
- Private endpoints (orders, portfolio, positions) require auth
- WebSocket connection itself requires auth even for public channels

### Key REST Endpoints (Public — No Auth)

**Get Markets (paginated)**
```
GET /markets?series_ticker=KXHIGHNY&status=open&limit=100
GET /markets?series_ticker=KXBTC15M&status=open&limit=100
```
Returns: ticker, title, open/close times, yes_bid, yes_ask, volume, result (after settlement).
Pagination via `cursor` param. Page sizes 1–1000 (default 100).
Filter by `min_updated_ts` to get only recently changed markets.

**Get Single Market**
```
GET /markets/{ticker}
# e.g., GET /markets/KXHIGHNY-26FEB07-T35
```

**Get Event (all brackets for one day)**
```
GET /events/{event_ticker}
# Returns mutual exclusivity info, all market metadata for the event
```

**Get Market Orderbook (no auth required)**
```
GET /markets/{ticker}/orderbook
# e.g., GET /markets/KXBTC15M-26FEB071830/orderbook
```
Returns YES bids and NO bids as `[price_cents, quantity]` arrays. Kalshi only returns bids (not asks) because in binary markets, a YES bid at X = NO ask at (100-X).

**Get Trades (paginated)**
```
GET /trades?ticker={ticker}&limit=1000&min_ts={unix_ts}&max_ts={unix_ts}
```
Returns every executed trade with price, quantity, timestamp. Essential for backtesting.

**Get Market Candlesticks**
```
GET /markets/{series_ticker}/{market_ticker}/candlesticks?start_ts=...&end_ts=...&period_interval=...
```
OHLC data for a market within a series.

### Key REST Endpoints (Authenticated)

**Place Order**
```
POST /portfolio/orders
{
  "ticker": "KXBTC15M-26FEB071830",
  "action": "buy",
  "side": "yes",
  "count": 10,
  "type": "limit",
  "yes_price": 55,
  "client_order_id": "<uuid>"
}
```

**Get Portfolio Balance**
```
GET /portfolio/balance
```

**Get Positions**
```
GET /portfolio/positions?ticker=KXBTC15M-26FEB071830
```

**Get Queue Positions**
```
GET /portfolio/orders/queue_positions?market_tickers=KXBTC15M-26FEB071830
```

### WebSocket Channels

All require authenticated WS connection. Some channels carry public data but auth is still needed for the handshake.

**Public channels:**
- `ticker` / `ticker_v2` — real-time yes_bid, yes_ask, last price, volume
- `trade` — every executed trade
- `market_lifecycle_v2` — market open/close/settle events
- `multivariate` — multi-market lookups

**Private channels:**
- `orderbook_delta` — full orderbook snapshot then incremental updates
- `fill` — your trade fills
- `market_positions` — your position updates
- `user_orders` — your order lifecycle events
- `communications` — RFQ/quote notifications

**Subscribe example:**
```json
{
  "id": 1,
  "cmd": "subscribe",
  "params": {
    "channels": ["orderbook_delta", "ticker"],
    "market_ticker": "KXBTC15M-26FEB071830"
  }
}
```

Can subscribe to multiple tickers with `market_tickers` (array). Use `update_subscription` with `action: "add_markets"` to add tickers to existing subscriptions without re-subscribing.

### Python SDK

```bash
pip install kalshi-python
```

```python
import kalshi_python

config = kalshi_python.Configuration(
    host="https://api.elections.kalshi.com/trade-api/v2"
)
config.api_key_id = "your-api-key-id"
config.private_key_pem = open('kalshi-key.pem').read()

client = kalshi_python.KalshiClient(config)

# Public data (no auth needed for raw requests, but SDK handles it)
market = client.get_market("KXHIGHNY-26FEB07-T35")
trades = client.get_trades(ticker="KXHIGHNY-26FEB07-T35", limit=100)
```

### Rate Limits

- REST: tiered limits, returns 429 on exceed
- WebSocket: real-time, no polling needed
- Demo environment available for testing: `demo-api.kalshi.co`
- Apply for "Advanced API Access" for higher rate limits

### Backtesting Data Collection

To pull ALL historical data for a series:
```python
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
cursor = None
all_markets = []

while True:
    params = {"series_ticker": "KXHIGHNY", "limit": 1000}
    if cursor:
        params["cursor"] = cursor
    resp = requests.get(f"{BASE}/markets", params=params).json()
    all_markets.extend(resp["markets"])
    cursor = resp.get("cursor")
    if not cursor:
        break

# For each market, pull trades
for market in all_markets:
    trades = requests.get(f"{BASE}/trades", params={
        "ticker": market["ticker"], "limit": 1000
    }).json()
```

---

## 2. WEATHER DATA SOURCES (for KXHIGHNY)

### 2A. NWS API (api.weather.gov) — Primary Forecast Source

Free, no API key required. JSON REST API. This is the official source that generates the forecasts most Kalshi traders watch.

**Step 1: Get grid coordinates for Central Park**
```
GET https://api.weather.gov/points/40.7829,-73.9654
```
Returns: grid office (OKX), gridX, gridY, and links to forecast endpoints.

**Step 2: Get hourly forecast (7 days)**
```
GET https://api.weather.gov/gridpoints/OKX/{gridX},{gridY}/forecast/hourly
```
Returns hourly temperature, wind, precipitation probability, short forecast text.

**Step 3: Get raw gridpoint data (most detailed)**
```
GET https://api.weather.gov/gridpoints/OKX/{gridX},{gridY}
```
Returns ALL forecast layers as time series: temperature, dewpoint, wind speed/direction, sky cover, precipitation probability/amount, snowfall, etc. Data is hourly for first ~72h. Time intervals in ISO 8601 format with duration (e.g., `2026-02-07T18:00:00+00:00/PT1H`).

**Step 4: Get current observations (KNYC station)**
```
GET https://api.weather.gov/stations/KNYC/observations/latest
GET https://api.weather.gov/stations/KNYC/observations?start=2026-02-07T00:00:00Z
```
Returns real-time observed temperature, which you can track throughout the day.

**Headers required:**
```
User-Agent: YourApp (your@email.com)
Accept: application/geo+json
```

**Key notes:**
- Observations may be delayed up to 20 minutes from MADIS upstream
- Temperature returned in Celsius; convert to Fahrenheit (the resolution unit)
- Cache the grid coordinates; they rarely change

### 2B. NWS Daily Climate Report (CLI) — Resolution Source

This is what Kalshi actually uses to settle markets. Published twice daily:
- Preliminary: ~4:30 PM local time (covers midnight to 4 PM)
- Final: ~1:30 AM next day (covers full 24h from midnight EST / 1 AM EDT)

**Machine-readable access:**
```
https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC
```
Returns a text product with observed MAX, MIN, AVG temperature, precipitation, snowfall, etc.

**Monthly F-6 form (historical daily data):**
```
https://forecast.weather.gov/product.php?site=NWS&product=CF6&issuedby=NYC
```
Contains daily MAX/MIN/AVG for the entire month — essential for backtesting.

**Raw text via NOAA:**
```
https://tgftp.nws.noaa.gov/data/raw/cd/cdus41.kokx.cli.nyc.txt
```

### 2C. NWS Time Series (KNYC Station) — Intraday Observations

Real-time temperature observations updated every few minutes:
```
https://www.weather.gov/wrh/timeseries?site=KNYC
```
Web page with charts. Underlying data accessible via NWS API observations endpoint (see 2A step 4).

Historical observations (past 3 days HTML table):
```
https://forecast.weather.gov/data/obhistory/KNYC.html
```

### 2D. Open-Meteo API — Multi-Model Comparison (THE KEY EDGE)

Free, no API key, no rate limits for non-commercial use. This is how you compare multiple weather models against each other to find divergence from market consensus.

**GFS + HRRR (best for US short-term):**
```
GET https://api.open-meteo.com/v1/gfs?latitude=40.7829&longitude=-73.9654&hourly=temperature_2m&temperature_unit=fahrenheit&forecast_days=2&models=gfs_seamless,hrrr_conus
```

**ECMWF IFS (European model, 9km resolution):**
```
GET https://api.open-meteo.com/v1/ecmwf?latitude=40.7829&longitude=-73.9654&hourly=temperature_2m&temperature_unit=fahrenheit
```

**Multi-model comparison in one call:**
```
GET https://api.open-meteo.com/v1/forecast?latitude=40.7829&longitude=-73.9654&hourly=temperature_2m&temperature_unit=fahrenheit&models=best_match,gfs_seamless,ecmwf_ifs025,icon_seamless,gem_seamless
```

**Ensemble API (probabilistic forecasts — gives uncertainty range):**
```
GET https://api.open-meteo.com/v1/ensemble?latitude=40.7829&longitude=-73.9654&hourly=temperature_2m&temperature_unit=fahrenheit&models=gfs_ensemble,ecmwf_ifs025_ensemble&forecast_days=2
```
Returns up to 30+ ensemble members per model. You can compute probability distributions for each temperature bracket directly from these.

**Key models available via Open-Meteo:**

| Model | Resolution | Update Freq | Forecast Range | Best For |
|-------|-----------|-------------|----------------|----------|
| HRRR | 3 km | Hourly | 18–48h | Short-term US (best for same-day) |
| GFS | 13–25 km | 6h | 16 days | Medium-range US |
| ECMWF IFS | 9 km | 6h | 10–15 days | Global, highly accurate |
| ICON | 13 km | 6h | 7 days | Global alternative |
| GEM | 25 km | 12h | 16 days | Canadian model |
| NBM (National Blend) | 2.5 km | Hourly | 264h | NWS consensus blend |

**15-minute data available** for HRRR via `&minutely_15=temperature_2m`.

**Historical forecast archive** (for backtesting model accuracy):
```
GET https://archive-api.open-meteo.com/v1/archive?latitude=40.7829&longitude=-73.9654&start_date=2025-01-01&end_date=2025-12-31&daily=temperature_2m_max,temperature_2m_min&temperature_unit=fahrenheit
```

### 2E. NOAA Climate Data Online — Historical Actuals

For backtesting: get historical actual high temperatures for Central Park.

**Station ID:** `GHCND:USW00094728` (NY City Central Park)

```
GET https://www.ncei.noaa.gov/cdo-web/api/v2/data?datasetid=GHCND&stationid=GHCND:USW00094728&datatypeid=TMAX&startdate=2025-01-01&enddate=2025-12-31&units=standard&limit=1000
```
Requires free API token from https://www.ncdc.noaa.gov/cdo-web/token

Returns daily TMAX (in tenths of °C by default; use `&units=standard` for Fahrenheit).

### Weather Strategy Implementation

The edge: compare forecasts from multiple models against the market-implied probability distribution.

```
1. Each morning, pull HRRR, GFS, ECMWF, NBM forecasts for Central Park max temp
2. Pull ensemble members to compute probability distribution across brackets
3. Pull current Kalshi bracket prices → implied probability for each bracket
4. Compare: if model ensemble says 40% chance of 34-35°F but market prices it at 25¢, buy
5. Throughout the day, monitor real-time observations from KNYC station
6. As actual temp readings come in, update probabilities and trade accordingly
7. Track NWS preliminary CLI report at ~4:30 PM for early resolution signal
```

---

## 3. BTC PRICE DATA SOURCES (for KXBTC15M)

### 3A. CF Benchmarks BRTI — The Actual Resolution Source

Kalshi settles crypto markets using the **CME CF Bitcoin Real-Time Index (BRTI)**. This is calculated once per second by aggregating order book data from constituent exchanges (Bitstamp, Coinbase, Gemini, itBit, Kraken, LMAX Digital).

Settlement uses a **1-minute window** of per-second observations at expiry, with **trimmed averaging** (top and bottom 20% excluded).

**Accessing BRTI:**
- CF Benchmarks website: https://www.cfbenchmarks.com/data/indices/BRTI (displays chart, no free API)
- CME Group data: available to CME subscribers
- **No free real-time API.** This is a paid institutional data product.

**Workaround:** Since BRTI aggregates the same major exchanges you can access directly, you can approximate it by monitoring those exchanges' order books. The key insight is that BRTI is very close to the volume-weighted midpoint across these exchanges, so tracking Binance/Coinbase/Kraken spot prices gives you a very close approximation.

### 3B. Binance WebSocket — Best Free Real-Time BTC Price

Binance has the highest BTC/USDT volume globally. While not an exact match for BRTI (which uses BTC/USD pairs on specific exchanges), Binance spot price tracks BRTI extremely closely (typically within $5-20).

**Trade stream (individual trades, no auth required):**
```
wss://stream.binance.com:9443/ws/btcusdt@trade
```
Message format:
```json
{
  "e": "trade",
  "E": 1672515782136,
  "s": "BTCUSDT",
  "t": 12345,
  "p": "69577.97",
  "q": "0.001",
  "T": 1672515782136
}
```

**Aggregated trade stream (batched, lower bandwidth):**
```
wss://stream.binance.com:9443/ws/btcusdt@aggTrade
```

**Mini ticker (1s updates with OHLC):**
```
wss://stream.binance.com:9443/ws/btcusdt@miniTicker
```

**Book ticker (best bid/ask, real-time):**
```
wss://stream.binance.com:9443/ws/btcusdt@bookTicker
```

**Kline/candlestick (1m candles):**
```
wss://stream.binance.com:9443/ws/btcusdt@kline_1m
```

**REST for historical data:**
```
GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000
GET https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT
```

No API key needed for public market data. Connection auto-disconnects after 24h; implement reconnect logic.

### 3C. Coinbase WebSocket — BRTI Constituent Exchange

Coinbase is a constituent exchange for BRTI calculation.

```
wss://ws-feed.exchange.coinbase.com
```
Subscribe:
```json
{
  "type": "subscribe",
  "product_ids": ["BTC-USD"],
  "channels": ["ticker"]
}
```

No auth required for public channels. Gives real-time last trade price, best bid/ask.

### 3D. Kraken WebSocket — Another BRTI Constituent

```
wss://ws.kraken.com/v2
```
Subscribe:
```json
{
  "method": "subscribe",
  "params": {
    "channel": "ticker",
    "symbol": ["BTC/USD"]
  }
}
```

### 3E. CoinGecko API — Free Historical Data for Backtesting

```
GET https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=90&interval=daily
GET https://api.coingecko.com/api/v3/coins/bitcoin/ohlc?vs_currency=usd&days=30
```
Free tier: 10-30 calls/min. Good for historical analysis, not real-time trading.

### BTC 15-Min Strategy Implementation

```
1. Connect to Binance WS for real-time BTC price (btcusdt@trade)
2. Optionally cross-reference with Coinbase/Kraken (actual BRTI constituents)
3. Subscribe to Kalshi WS for KXBTC15M orderbook + ticker
4. For each 15-min window:
   a. Record the opening price (from Kalshi market metadata or BRTI)
   b. Track current price vs opening price throughout the window
   c. Compute implied probability of UP vs DOWN based on current price movement
   d. Compare to Kalshi market price
   e. If Kalshi price diverges significantly from your fair value → trade
5. Maker strategy: post limit orders on both sides, collect spread
6. Arbitrage check: if YES_ask + NO_ask < 100¢, buy both sides (risk-free)
```

---

## 4. CROSS-REFERENCE SUMMARY

### For KXHIGHNY (Weather)

| Source | What It Gives You | Latency | Auth | Cost |
|--------|-------------------|---------|------|------|
| **Kalshi REST** | Historical markets, trades, prices, resolutions | ~100ms | No (public) | Free |
| **Kalshi WS** | Real-time orderbook, trades, ticker | Real-time | Yes | Free |
| **NWS API (api.weather.gov)** | Official NWS forecast, hourly/gridpoint data | ~minutes | No (User-Agent only) | Free |
| **NWS Observations (KNYC)** | Real-time actual temperature readings | ~20min delay | No | Free |
| **NWS CLI Report** | Final daily high (resolution source) | ~4:30PM / 1:30AM | No | Free |
| **Open-Meteo GFS/HRRR** | Multi-model temperature forecasts, hourly updates | ~minutes | No | Free |
| **Open-Meteo ECMWF** | European model forecast (often most accurate) | ~minutes | No | Free |
| **Open-Meteo Ensemble** | Probabilistic ensemble (30+ members) | ~minutes | No | Free |
| **NOAA CDO** | Historical actual daily temperatures (backtesting) | Hours | Token (free) | Free |

### For KXBTC15M (BTC 15-Min)

| Source | What It Gives You | Latency | Auth | Cost |
|--------|-------------------|---------|------|------|
| **Kalshi REST** | Historical markets, trades, prices, resolutions | ~100ms | No (public) | Free |
| **Kalshi WS** | Real-time orderbook for current 15-min market | Real-time | Yes | Free |
| **Binance WS (btcusdt@trade)** | Real-time BTC spot price (highest volume) | <50ms | No | Free |
| **Coinbase WS (BTC-USD)** | Real-time BTC/USD (BRTI constituent) | <100ms | No | Free |
| **Kraken WS (BTC/USD)** | Real-time BTC/USD (BRTI constituent) | <100ms | No | Free |
| **Binance REST (klines)** | Historical 1m candles for backtesting | ~100ms | No | Free |
| **CF Benchmarks (BRTI)** | Exact resolution index (per-second) | N/A | Paid | $$$ |

---

## 5. IMPLEMENTATION ARCHITECTURE

### Phase 1: Data Collection & Backtesting (No money at risk)

```
kalshi-bot/
├── config/
│   ├── kalshi_credentials.py     # API key + private key path
│   └── settings.py               # Market tickers, intervals, thresholds
├── data/
│   ├── collectors/
│   │   ├── kalshi_historical.py  # Pull all KXHIGHNY + KXBTC15M history
│   │   ├── nws_forecast.py       # Poll api.weather.gov every hour
│   │   ├── nws_observations.py   # Poll KNYC station every 5 min
│   │   ├── open_meteo.py         # Pull multi-model forecasts
│   │   ├── btc_price_logger.py   # Binance WS → log to DB
│   │   └── noaa_historical.py    # Bulk download for backtesting
│   └── storage/
│       └── db.py                 # SQLite or PostgreSQL schema
├── models/
│   ├── weather_model.py          # Ensemble → bracket probability distribution
│   └── btc_model.py              # Price feed → fair value for YES/NO
├── backtest/
│   ├── weather_backtest.py       # Compare model predictions to Kalshi resolutions
│   └── btc_backtest.py           # Analyze historical YES+NO spreads, edge sizing
└── analysis/
    ├── edge_report.py            # Quantify edge per strategy
    └── visualize.py              # Plot model vs market vs actual
```

### Phase 2: Live Trading (Paper → Real)

```
├── trading/
│   ├── kalshi_client.py          # Authenticated REST + WS wrapper
│   ├── weather_trader.py         # Weather strategy execution
│   ├── btc_trader.py             # BTC 15-min strategy execution
│   └── risk_manager.py           # Position limits, daily P&L stops
├── signals/
│   ├── weather_signal.py         # Model divergence → trade signal
│   └── btc_signal.py             # Price feed → fair value → trade signal
└── monitoring/
    ├── dashboard.py              # Real-time P&L, positions, signals
    └── alerts.py                 # Discord/SMS on errors or big moves
```

### Key Technical Decisions

**Database:** PostgreSQL for production (time-series queries, concurrent access) or SQLite for prototyping.

**Async framework:** `asyncio` + `websockets` for concurrent WS connections to Kalshi + Binance + Coinbase.

**Scheduling:** 
- Weather: cron job every hour to pull new model runs, every 5 min for observations
- BTC: continuous WebSocket streams, react in <1 second

**Paper trading:** Use Kalshi's demo environment (`demo-api.kalshi.co`) to test execution without real money. All endpoints work identically.

---

## 6. QUICK-START COMMANDS

### Test Kalshi Public API (no auth needed)

```bash
# Get today's NYC high temp markets
curl "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXHIGHNY&status=open&limit=10" | python -m json.tool

# Get today's BTC 15-min markets
curl "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=10" | python -m json.tool

# Get orderbook for a specific market
curl "https://api.elections.kalshi.com/trade-api/v2/markets/KXHIGHNY-26FEB07-T35/orderbook" | python -m json.tool
```

### Test NWS API

```bash
# Get Central Park grid info
curl -H "User-Agent: KalshiBot (you@email.com)" "https://api.weather.gov/points/40.7829,-73.9654" | python -m json.tool

# Get hourly forecast
curl -H "User-Agent: KalshiBot (you@email.com)" "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly" | python -m json.tool

# Get latest observation
curl -H "User-Agent: KalshiBot (you@email.com)" "https://api.weather.gov/stations/KNYC/observations/latest" | python -m json.tool
```

### Test Open-Meteo (no auth, no headers)

```bash
# Multi-model temperature forecast for Central Park
curl "https://api.open-meteo.com/v1/forecast?latitude=40.7829&longitude=-73.9654&hourly=temperature_2m&temperature_unit=fahrenheit&models=best_match,gfs_seamless,ecmwf_ifs025,icon_seamless&forecast_days=2" | python -m json.tool

# Ensemble forecast (probabilistic)
curl "https://api.open-meteo.com/v1/ensemble?latitude=40.7829&longitude=-73.9654&hourly=temperature_2m&temperature_unit=fahrenheit&models=gfs_ensemble,ecmwf_ifs025_ensemble" | python -m json.tool
```

### Test Binance (no auth)

```bash
# Current BTC price
curl "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# Last 100 1-minute candles
curl "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=100"
```

---

## 7. CRITICAL DETAILS

### Kalshi Fee Structure

Taker fee per contract: `$0.07 × P × (1-P)`, rounded up to nearest cent. Maker fee: **$0**.

| Contract Price | Taker Fee | Fee as % of Price |
|---------------|-----------|-------------------|
| 5¢ | 1¢ | 20.0% |
| 10¢ | 1¢ | 10.0% |
| 25¢ | 2¢ | 8.0% |
| 50¢ | 2¢ | 4.0% |
| 75¢ | 2¢ | 2.7% |
| 90¢ | 1¢ | 1.1% |

**Implication:** Always prefer maker orders (limit orders that rest on the book). The fee difference between maker ($0) and taker ($0.01-$0.02) is often the entire edge.

### Kalshi Orderbook Mechanics

- Only bids are returned (YES bids + NO bids)
- YES bid at X¢ = NO ask at (100-X)¢ and vice versa
- Spread = best YES ask - best YES bid = (100 - best NO bid) - best YES bid
- When spread = 0, market is locked (no edge for market-making)

### Weather Market Timing

- Markets open: typically available 2-3 days in advance
- Trading active: peaks during morning (model runs) and afternoon (real observations)
- Resolution: NWS Daily Climate Report, published ~1:30 AM next day
- During Daylight Saving Time: high temp recorded 1:00 AM to 12:59 AM local (next day)
- During Standard Time: midnight to midnight

### BTC 15-Min Market Timing

- New market every 15 minutes, 24/7
- Each market has a defined open time and close time
- Settlement: 1-minute BRTI average at close, with trimmed mean
- Trading window: can trade during the 15-min window before close
- Key: the opening reference price is locked at market creation time
