# Kalshi Bot — Implementation Plan

## Overview

This plan covers 12 improvements organized into 4 tiers by priority and dependency order. Each section includes the rationale, the specific changes needed, which files are affected, and acceptance criteria so you know when it's done.

Estimated total effort: ~40–60 hours across all tiers.

---

## Tier 1: Critical Path (Do First)

These are structural changes that directly determine whether the bot can generate real edge. Everything else builds on top of these.

---

### 1.1 WebSocket Integration for Real-Time Data

**Why:** REST polling for BTC 15-minute markets is a structural handicap. You're competing against bots that react in <100ms. With a 5-second poll interval, your average latency is 2.5 seconds and you miss intra-poll price movements entirely. WebSocket gives you every tick as it happens.

**Scope:** Two persistent WebSocket connections running concurrently via asyncio — one to Kalshi, one to Binance. Kalshi WS also replaces polling for weather market orderbook updates (lower priority but free once the infrastructure exists).

#### 1.1.1 WebSocket Connection Manager

Create a new module that handles connection lifecycle, authentication, reconnection, and message dispatch.

**New file:** `kalshi_pipeline/ws/manager.py`

```
class WSManager:
    - __init__(url, auth_headers, on_message, on_error, reconnect_delay)
    - connect()          → establishes WS, sends auth headers
    - subscribe(channels, tickers)  → sends subscribe command
    - unsubscribe(sids)  → sends unsubscribe command
    - _listen_loop()     → async for message in ws: dispatch to on_message
    - _reconnect()       → exponential backoff (1s, 2s, 4s, 8s, max 60s)
    - _heartbeat()       → respond to server pings, send unsolicited pongs every 30s
    - close()            → graceful shutdown
```

Key implementation details:
- Use `websockets` library (already async-native)
- Kalshi WS auth: sign `GET/trade-api/ws/v2` with RSA-PSS, pass headers during handshake
- Kalshi WS URL: `wss://api.elections.kalshi.com/trade-api/ws/v2` (production) or `wss://demo-api.kalshi.co/trade-api/ws/v2` (demo)
- Binance WS URL: `wss://stream.binance.com:9443/ws/btcusdt@trade` (no auth needed)
- Binance auto-disconnects after 24h — reconnect handler must account for this
- Track sequence numbers (`seq` field in Kalshi messages) to detect dropped messages

#### 1.1.2 Kalshi WebSocket Feed

**New file:** `kalshi_pipeline/ws/kalshi_feed.py`

```
class KalshiFeed:
    - __init__(ws_manager, db_pool, signal_callback)
    - subscribe_market(ticker)       → subscribe to orderbook_delta + ticker channels
    - subscribe_lifecycle()          → subscribe to market_lifecycle_v2 (detect new contracts)
    - _handle_orderbook_snapshot(msg) → rebuild local orderbook state
    - _handle_orderbook_delta(msg)    → apply incremental update to local orderbook
    - _handle_ticker(msg)            → update best bid/ask, detect spread changes
    - _handle_lifecycle(msg)         → detect new BTC 15-min contracts opening
    - get_orderbook(ticker)          → return current local orderbook state
    - get_best_bid_ask(ticker)       → return (yes_bid, yes_ask) from local state
```

Local orderbook state management:
- On `orderbook_snapshot`: replace entire book for that ticker
- On `orderbook_delta`: apply price/delta update. If delta=0, remove that price level
- Store as `dict[ticker, {"yes": {price: qty}, "no": {price: qty}}]`
- The `yes_ask` at price X equals `100 - best_no_bid` (Kalshi only sends bids)

Subscribe to these channels:
- `orderbook_delta` — full book state, required for spread/arb detection
- `ticker` — lightweight, gives yes_bid/yes_ask/last_price/volume
- `market_lifecycle_v2` — tells you when new 15-min BTC contracts open (so you can auto-subscribe)

#### 1.1.3 Binance WebSocket Feed

**New file:** `kalshi_pipeline/ws/binance_feed.py`

```
class BinanceFeed:
    - __init__(on_price_update)
    - connect()                → connect to wss://stream.binance.com:9443/ws/btcusdt@trade
    - _handle_trade(msg)       → extract price, quantity, timestamp
    - get_latest_price()       → return most recent trade price
    - get_vwap(window_seconds) → compute VWAP over recent window
    - get_price_history(n)     → return last N prices for momentum calc
```

Message format from `btcusdt@trade`:
```json
{"e":"trade", "s":"BTCUSDT", "p":"97500.50", "q":"0.001", "T":1707327420136}
```

Store a rolling buffer of the last 900 trades (roughly 15 minutes of data at normal volume). This buffer feeds the BTC signal model for momentum calculation.

#### 1.1.4 Integrate into Main Loop

**Modified file:** `kalshi_pipeline/main.py`

The main worker loop needs to become async. Current structure (simplified):

```python
# BEFORE
while True:
    poll_cycle()    # REST calls, sync
    time.sleep(POLL_INTERVAL)
```

New structure:

```python
# AFTER
async def main():
    # Start WS connections
    kalshi_ws = KalshiFeed(...)
    binance_ws = BinanceFeed(...)

    # Run WS feeds + periodic tasks concurrently
    await asyncio.gather(
        kalshi_ws.run(),          # persistent WS listener
        binance_ws.run(),         # persistent WS listener
        periodic_poll_loop(),     # REST fallback every 60s for health checks
        signal_generation_loop(), # evaluate signals every N seconds using WS data
    )
```

The `periodic_poll_loop` keeps your existing REST-based collection as a fallback and health check — it verifies WS state matches REST state every 60 seconds and alerts on divergence.

#### 1.1.5 Coinbase + Kraken Feeds (BRTI Constituents)

**New file:** `kalshi_pipeline/ws/coinbase_feed.py`
**New file:** `kalshi_pipeline/ws/kraken_feed.py`

Same pattern as BinanceFeed but for BRTI constituent exchanges.

Coinbase (`wss://ws-feed.exchange.coinbase.com`):
```json
{"type":"subscribe","product_ids":["BTC-USD"],"channels":["ticker"]}
→ {"type":"ticker","price":"97500.50","best_bid":"97499.00","best_ask":"97501.00",...}
```

Kraken (`wss://ws.kraken.com/v2`):
```json
{"method":"subscribe","params":{"channel":"ticker","symbol":["BTC/USD"]}}
```

These feeds provide BTC/USD prices (not BTC/USDT like Binance), which is what BRTI actually uses.

**Acceptance criteria:**
- [ ] Kalshi WS connects, authenticates, subscribes to orderbook_delta for current BTC market
- [ ] Binance WS connects and streams BTC trades into rolling buffer
- [ ] Coinbase + Kraken WS connect and provide BTC/USD prices
- [ ] All WS connections auto-reconnect on disconnect with exponential backoff
- [ ] Local orderbook state matches REST orderbook fetch (verified by periodic check)
- [ ] New BTC 15-min contracts are auto-detected via market_lifecycle_v2
- [ ] Main loop is fully async, REST polling demoted to fallback role

---

### 1.2 Ensemble-Based Weather Probability Model

**Why:** A single point forecast gives you one temperature prediction. The market prices a probability distribution across 6+ brackets. You need your own probability distribution to find mispriced brackets. The ensemble API gives you 30+ independent model runs, from which you can directly compute bracket probabilities.

#### 1.2.1 Multi-Model Ensemble Collector

**Modified file:** `kalshi_pipeline/collectors/weather.py`

Replace or augment the current forecast collection with ensemble pulls from multiple models.

API calls to make (all free, no auth):

```
# GFS ensemble (30 members)
GET https://api.open-meteo.com/v1/ensemble
  ?latitude=40.7829&longitude=-73.9654
  &hourly=temperature_2m
  &temperature_unit=fahrenheit
  &models=gfs_ensemble
  &forecast_days=2

# ECMWF ensemble (50 members)
GET https://api.open-meteo.com/v1/ensemble
  ?latitude=40.7829&longitude=-73.9654
  &hourly=temperature_2m
  &temperature_unit=fahrenheit
  &models=ecmwf_ifs025_ensemble
  &forecast_days=2

# Deterministic models for cross-reference
GET https://api.open-meteo.com/v1/forecast
  ?latitude=40.7829&longitude=-73.9654
  &hourly=temperature_2m
  &temperature_unit=fahrenheit
  &models=best_match,gfs_seamless,ecmwf_ifs025,icon_seamless,hrrr_conus
  &forecast_days=2
```

Response structure for ensemble endpoint:
```json
{
  "hourly": {
    "time": ["2026-02-08T00:00", "2026-02-08T01:00", ...],
    "temperature_2m_member00": [28.5, 27.1, ...],
    "temperature_2m_member01": [29.0, 27.8, ...],
    ...
    "temperature_2m_member29": [27.2, 26.5, ...]
  }
}
```

For each member, extract the daily maximum temperature (max of all hourly values for the target date, respecting the NWS measurement window — see 1.2.3 below).

#### 1.2.2 Bracket Probability Calculator

**Modified file:** `kalshi_pipeline/signals/weather.py`

New function that replaces the current signal logic:

```python
def compute_bracket_probabilities(
    ensemble_max_temps: list[float],   # e.g., 80 values (30 GFS + 50 ECMWF)
    brackets: list[dict]               # e.g., [{"ticker": "...", "low": 30, "high": 32}, ...]
) -> dict[str, float]:
    """
    Given ensemble of predicted daily max temperatures and market brackets,
    compute probability for each bracket.

    Returns: {"KXHIGHNY-26FEB08-T30": 0.12, "KXHIGHNY-26FEB08-T32": 0.35, ...}
    """
    total = len(ensemble_max_temps)
    probs = {}
    for bracket in brackets:
        count = sum(
            1 for t in ensemble_max_temps
            if bracket["low"] <= t < bracket["high"]
        )
        probs[bracket["ticker"]] = count / total
    return probs
```

Then compare these model-derived probabilities against market-implied probabilities:

```python
def find_edges(
    model_probs: dict[str, float],
    market_snapshots: dict[str, dict]  # ticker → {yes_bid, yes_ask, ...}
) -> list[Signal]:
    signals = []
    for ticker, model_p in model_probs.items():
        snap = market_snapshots.get(ticker)
        if not snap:
            continue
        market_p = snap["yes_ask"] / 100.0  # implied prob from ask price
        edge = model_p - market_p
        if abs(edge) > MIN_EDGE_THRESHOLD:  # e.g., 0.05 (5%)
            signals.append(Signal(
                ticker=ticker,
                direction="buy_yes" if edge > 0 else "buy_no",
                model_prob=model_p,
                market_prob=market_p,
                edge_bps=int(edge * 10000),
                confidence=compute_confidence(model_p, len(ensemble_max_temps)),
            ))
    return signals
```

#### 1.2.3 NWS Measurement Window Handling

The NWS Daily Climate Report for Central Park measures the daily high using a specific time window that differs by season:

- **During Daylight Saving Time (Mar–Nov):** 1:00 AM to 12:59 AM (next day)
- **During Standard Time (Nov–Mar):** Midnight to midnight

When extracting daily max from ensemble hourly data, you must use the correct window. The Open-Meteo API returns data in the timezone you specify (add `&timezone=America/New_York`). Filter hourly values to the correct measurement window before taking the max.

```python
def extract_daily_max(hourly_temps: list[float], hourly_times: list[str], target_date: str, is_dst: bool) -> float:
    """Extract daily max using NWS measurement window."""
    if is_dst:
        # 1:00 AM today to 12:59 AM tomorrow
        start = f"{target_date}T01:00"
        end_date = (datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        end = f"{end_date}T00:00"
    else:
        # midnight to midnight
        start = f"{target_date}T00:00"
        end = f"{target_date}T23:59"

    temps_in_window = [
        t for t, time in zip(hourly_temps, hourly_times)
        if start <= time <= end
    ]
    return max(temps_in_window) if temps_in_window else None
```

#### 1.2.4 Store Ensemble Data for Backtesting

**Modified file:** `kalshi_pipeline/schema.sql`

Your existing `weather_ensemble_samples` table should store individual member predictions, not aggregated values:

```sql
CREATE TABLE IF NOT EXISTS weather_ensemble_forecasts (
    id SERIAL PRIMARY KEY,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_date DATE NOT NULL,
    model TEXT NOT NULL,          -- 'gfs_ensemble', 'ecmwf_ifs025_ensemble'
    member_index INT NOT NULL,    -- 0-29 for GFS, 0-49 for ECMWF
    predicted_max_f NUMERIC,     -- predicted daily max in Fahrenheit
    forecast_hour INT,            -- hours ahead of the forecast run
    UNIQUE(collected_at, target_date, model, member_index)
);

-- Also store the computed bracket probabilities each cycle
CREATE TABLE IF NOT EXISTS weather_bracket_probs (
    id SERIAL PRIMARY KEY,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_date DATE NOT NULL,
    ticker TEXT NOT NULL,
    bracket_low NUMERIC,
    bracket_high NUMERIC,
    model_prob NUMERIC,          -- ensemble-derived probability
    market_prob NUMERIC,         -- market-implied probability at compute time
    edge NUMERIC,                -- model_prob - market_prob
    ensemble_count INT,          -- total ensemble members used
    UNIQUE(computed_at, ticker)
);
```

**Acceptance criteria:**
- [ ] Ensemble data pulled from both GFS (30 members) and ECMWF (50 members)
- [ ] Daily max extracted using correct NWS measurement window (DST-aware)
- [ ] Bracket probabilities computed from 80 ensemble members
- [ ] Edge calculated as model_prob minus market_prob for each bracket
- [ ] Signals generated only when edge exceeds threshold
- [ ] All ensemble data persisted for backtesting
- [ ] Deterministic model forecasts (HRRR, GFS, ECMWF, ICON) also collected for cross-reference

---

### 1.3 Resolution Tracking and Backtesting Accuracy

**Why:** You cannot justify going live until you can prove statistically that your model's predicted probabilities are better calibrated than the market's implied probabilities. This requires tracking every prediction you make against the actual outcome.

#### 1.3.1 Resolution Collector

**New file:** `kalshi_pipeline/collectors/resolutions.py`

Two data sources to parse:

**For weather (NWS CLI report):**

```python
import requests
import re

def fetch_nws_cli_nyc() -> dict:
    """
    Fetch and parse the NWS Daily Climate Report for NYC.
    Published ~4:30 PM (preliminary) and ~1:30 AM next day (final).
    """
    url = "https://forecast.weather.gov/product.php?site=OKX&product=CLI&issuedby=NYC"
    resp = requests.get(url, headers={"User-Agent": "KalshiBot (your@email.com)"})
    text = resp.text

    # Parse the text product for MAXIMUM TEMPERATURE
    # Format varies but typically:
    #   MAXIMUM TEMPERATURE (F)
    #     TODAY                     35
    #     RECORD                    62  1990
    max_match = re.search(r'MAXIMUM TEMPERATURE.*?TODAY\s+(\d+)', text, re.DOTALL)
    if max_match:
        return {"max_temp_f": int(max_match.group(1)), "source": "nws_cli"}
    return None
```

**For Kalshi (market result field):**

```python
def fetch_market_resolution(ticker: str) -> dict:
    """Check if a market has settled and get the result."""
    resp = requests.get(f"{KALSHI_BASE}/markets/{ticker}")
    market = resp.json()["market"]
    if market["status"] == "settled":
        return {
            "result": market["result"],         # "yes" or "no" or scalar
            "settled_at": market["close_time"],
        }
    return None
```

Run this collector on a schedule:
- Weather: check CLI at 5:00 PM and 2:00 AM daily
- BTC: check settled markets every 20 minutes (they settle 15 min after close)
- Kalshi market status: poll recently-closed markets for result field

#### 1.3.2 Resolution and Accuracy Schema

**Modified file:** `kalshi_pipeline/schema.sql`

```sql
-- Track actual resolution outcomes
CREATE TABLE IF NOT EXISTS market_resolutions (
    ticker TEXT PRIMARY KEY,
    series_ticker TEXT,
    event_ticker TEXT,
    market_type TEXT,             -- 'weather' or 'btc_15m'
    resolved_at TIMESTAMPTZ,
    result TEXT,                  -- 'yes', 'no', or scalar value
    actual_value NUMERIC,        -- actual temp (F) or BTC price ($)
    resolution_source TEXT,      -- 'nws_cli', 'kalshi_api', 'cf_brti'
    collected_at TIMESTAMPTZ DEFAULT NOW()
);

-- Track prediction accuracy per signal
CREATE TABLE IF NOT EXISTS prediction_accuracy (
    id SERIAL PRIMARY KEY,
    signal_id INT REFERENCES signals(id),
    ticker TEXT NOT NULL,
    signal_time TIMESTAMPTZ,
    model_prob NUMERIC,          -- your model's P(YES) at signal time
    market_prob NUMERIC,         -- market's implied P(YES) at signal time
    edge_bps INT,                -- edge in basis points
    actual_outcome BOOLEAN,      -- did YES win?
    pnl_per_contract NUMERIC,   -- actual P&L (100 if YES won and you bought YES, etc.)
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### 1.3.3 Backtesting Report Generator

**New file:** `kalshi_pipeline/analysis/accuracy_report.py`

Compute these metrics from the `prediction_accuracy` table:

```python
def generate_accuracy_report(market_type: str, days: int = 30) -> dict:
    """
    Compute model calibration and profitability metrics.

    Returns:
        brier_score:       Mean squared error of probability predictions (lower = better)
        log_loss:          Cross-entropy loss (lower = better)
        calibration_curve: Predicted prob buckets vs actual frequency
        edge_reliability:  % of signals where edge > 0 that were actually profitable
        total_pnl:         Simulated P&L from all signals (1 contract each)
        sharpe_ratio:      Risk-adjusted return
        n_signals:         Total signals evaluated
        hit_rate:          % of signals that were directionally correct
    """
```

Key questions this answers:
- "When my model says 40% probability, does the event actually happen ~40% of the time?" (calibration)
- "When my model says the market is mispriced by 10%, do I actually profit?" (edge reliability)
- "What is the expected P&L per signal, net of fees?" (profitability)

**You should not go live until:**
- Brier score is lower than market-implied probabilities' Brier score
- Edge reliability > 55% (i.e., more than half your "edge" signals are actually profitable)
- At least 100 resolved signals for statistical significance
- Expected P&L per signal > taker fee (even though you'll use maker orders)

**Acceptance criteria:**
- [ ] NWS CLI parser extracts daily max temperature for Central Park
- [ ] Kalshi market results collected automatically for settled markets
- [ ] Every signal is linked to its resolution outcome
- [ ] Brier score, log-loss, calibration curve, and P&L computed automatically
- [ ] Report can be generated via CLI command or Telegram `/accuracy` command
- [ ] At least 2 weeks of data collected before considering live trading

---

## Tier 2: Execution Quality (Do Second)

These improve trade execution and risk management. They matter most when transitioning from paper to live trading.

---

### 2.1 Order Placement Strategy Improvements

**Modified file:** `kalshi_pipeline/paper_trading.py`

#### 2.1.1 Maker-Only Order Policy

Current behavior (from report): converts snapshot prices to cent limits with min/max clamps.

Required behavior: always place limit orders that rest on the book (maker). Never cross the spread.

```python
def compute_order_price(signal: Signal, orderbook: dict) -> int:
    """
    Compute limit price that will rest on the book (maker order).
    Returns price in cents.
    """
    if signal.direction == "buy_yes":
        # Place YES bid at or below the current best YES bid
        # This ensures we're a maker, not a taker
        best_yes_bid = get_best_yes_bid(orderbook)
        best_yes_ask = get_best_yes_ask(orderbook)  # = 100 - best_no_bid

        if best_yes_ask - best_yes_bid <= 1:
            # Spread is locked (1¢ or 0¢). Can't be maker without matching.
            # Either join the bid or skip.
            return best_yes_bid  # join existing bid queue

        # Place 1¢ above best bid (improves our queue position)
        # but still below ask (stays maker)
        return min(best_yes_bid + 1, best_yes_ask - 1)

    elif signal.direction == "buy_no":
        best_no_bid = get_best_no_bid(orderbook)
        best_no_ask = get_best_no_ask(orderbook)
        if best_no_ask - best_no_bid <= 1:
            return best_no_bid
        return min(best_no_bid + 1, best_no_ask - 1)
```

#### 2.1.2 Arbitrage Detection

Check before every directional trade:

```python
def check_arbitrage(orderbook: dict) -> Optional[dict]:
    """
    Check if YES_ask + NO_ask < 100 (risk-free profit).
    Returns arbitrage opportunity or None.
    """
    best_yes_ask = 100 - max(p for p, q in orderbook["no"])  # implied from NO bids
    best_no_ask = 100 - max(p for p, q in orderbook["yes"])  # implied from YES bids

    total_cost = best_yes_ask + best_no_ask
    if total_cost < 100:
        return {
            "type": "arbitrage",
            "yes_price": best_yes_ask,
            "no_price": best_no_ask,
            "profit_per_contract": 100 - total_cost,  # in cents
            "contracts": min(
                sum(q for p, q in orderbook["no"] if (100 - p) == best_yes_ask),
                sum(q for p, q in orderbook["yes"] if (100 - p) == best_no_ask),
            )
        }
    return None
```

Arbitrage is rare on Kalshi (maybe a few times per week on thin markets) but when it appears it's literally free money. Always check before placing directional bets.

#### 2.1.3 Queue Position Monitoring

**New addition to order management:**

```python
async def monitor_queue_positions(open_orders: list[dict]):
    """
    Check queue position for resting orders.
    Cancel and re-place if queue position is too deep.
    """
    tickers = [o["ticker"] for o in open_orders]
    resp = await kalshi_client.get(
        "/portfolio/orders/queue_positions",
        params={"market_tickers": tickers}
    )

    for order in open_orders:
        queue_pos = resp.get(order["order_id"], {}).get("queue_position")
        if queue_pos and queue_pos > MAX_QUEUE_DEPTH:  # e.g., 50
            # Cancel and re-place at a more aggressive price
            await kalshi_client.cancel_order(order["order_id"])
            # Re-evaluate: is the edge still there?
            # If yes, re-place at improved price
```

#### 2.1.4 Order Lifecycle Management

Track every order through its full lifecycle:

```
placed → resting → [partially_filled] → filled | canceled | expired
```

For each state transition, log to `paper_trade_orders` with timestamp. This gives you fill rate analytics: "What % of my maker orders actually fill? What's the average time to fill?"

**Acceptance criteria:**
- [ ] All orders placed as maker (limit orders inside the spread)
- [ ] Arbitrage checked before every directional trade
- [ ] Queue position monitored for resting orders; stale orders canceled
- [ ] Order lifecycle fully tracked with state transitions and timestamps
- [ ] Fill rate analytics available (% of orders filled, avg fill time)

---

### 2.2 Smooth Confidence Degradation (Replace Hard Quorum Gate)

**Modified file:** `kalshi_pipeline/signals/btc.py`

#### 2.2.1 Replace Binary Gate with Weighted Confidence

Current (from report): `BTC_MIN_CORE_SOURCES` — if fewer than N sources available, no signal generated.

Problem: If Coinbase has a 30-second API hiccup, you miss an entire 15-minute trading window.

New approach:

```python
# Source weights (sum to 1.0 when all available)
SOURCE_WEIGHTS = {
    "coinbase": 0.30,   # BRTI constituent, BTC/USD
    "kraken": 0.20,     # BRTI constituent, BTC/USD
    "bitstamp": 0.15,   # BRTI constituent, BTC/USD
    "binance": 0.25,    # highest volume, but BTC/USDT
    "gemini": 0.10,     # BRTI constituent, lower volume
}

def compute_btc_fair_value(prices: dict[str, float]) -> tuple[float, float]:
    """
    Compute weighted fair value and confidence from available sources.

    Returns: (fair_value, confidence)
        - confidence in [0, 1], degrades smoothly with fewer sources
    """
    available_weight = sum(SOURCE_WEIGHTS[s] for s in prices)
    if available_weight == 0:
        return None, 0.0

    # Weighted average price
    fair_value = sum(prices[s] * SOURCE_WEIGHTS[s] for s in prices) / available_weight

    # Confidence = fraction of total weight available × agreement factor
    # Agreement factor penalizes when sources disagree
    if len(prices) >= 2:
        max_spread = max(prices.values()) - min(prices.values())
        agreement = max(0, 1.0 - (max_spread / fair_value) * 100)  # penalize >1% spread
    else:
        agreement = 0.7  # single source gets reduced confidence

    confidence = available_weight * agreement
    return fair_value, confidence
```

Then in signal generation, use confidence as a multiplier on position size rather than a hard gate:

```python
# Instead of: if num_sources < MIN_CORE_SOURCES: return None
# Do:
signal.confidence = confidence  # 0.0 to 1.0
signal.suggested_size = base_size * confidence  # scale position with confidence
```

**Acceptance criteria:**
- [ ] Signal generation continues with partial source availability
- [ ] Confidence score degrades smoothly based on source count and agreement
- [ ] Position sizing scales with confidence
- [ ] Logging shows which sources contributed to each signal

---

### 2.3 Position Sizing with Kelly Criterion

**New file:** `kalshi_pipeline/risk.py`

#### 2.3.1 Kelly Fraction Calculator

```python
def kelly_fraction(model_prob: float, market_price_cents: int, side: str) -> float:
    """
    Compute optimal Kelly fraction for a binary Kalshi bet.

    For a YES bet at price p (in cents):
        Win payoff = (100 - p) cents
        Loss = p cents
        Kelly f* = (model_prob * win - (1 - model_prob) * loss) / win

    Returns fraction of bankroll to bet (0.0 to 1.0).
    Use half-Kelly (multiply by 0.5) or quarter-Kelly for safety.
    """
    p = market_price_cents / 100.0  # convert to probability scale

    if side == "buy_yes":
        win = (100 - market_price_cents)  # profit if YES wins
        loss = market_price_cents          # loss if NO wins
        edge = model_prob * win - (1 - model_prob) * loss
    else:  # buy_no
        win = market_price_cents
        loss = (100 - market_price_cents)
        edge = (1 - model_prob) * win - model_prob * loss

    if edge <= 0:
        return 0.0

    kelly = edge / win
    return kelly
```

#### 2.3.2 Position Sizing Integration

```python
KELLY_FRACTION = 0.25           # quarter-Kelly (conservative)
MAX_POSITION_DOLLARS = 50.0     # hard cap per market
MAX_PORTFOLIO_EXPOSURE = 500.0  # hard cap total
MIN_ORDER_SIZE = 1              # minimum 1 contract

def compute_order_size(
    signal: Signal,
    bankroll: float,
    current_exposure: float,
) -> int:
    """Compute number of contracts to trade."""
    kelly = kelly_fraction(signal.model_prob, signal.market_price_cents, signal.direction)
    target_dollars = bankroll * kelly * KELLY_FRACTION * signal.confidence

    # Apply caps
    target_dollars = min(target_dollars, MAX_POSITION_DOLLARS)
    target_dollars = min(target_dollars, MAX_PORTFOLIO_EXPOSURE - current_exposure)

    # Convert to contracts
    price_per_contract = signal.market_price_cents / 100.0
    num_contracts = int(target_dollars / price_per_contract)

    return max(num_contracts, MIN_ORDER_SIZE) if num_contracts > 0 else 0
```

**Acceptance criteria:**
- [ ] Kelly fraction computed for every signal
- [ ] Position sizes scale with edge magnitude and confidence
- [ ] Hard caps on per-market and total portfolio exposure
- [ ] Quarter-Kelly used by default (configurable in config.py)

---

## Tier 3: Operational Quality (Do Third)

These improve monitoring, debugging, and day-to-day management of the live bot.

---

### 3.1 Telegram Command Interface

**Modified file:** `kalshi_pipeline/notifications.py`

Add an incoming message handler using python-telegram-bot's `Application` or a simple webhook.

#### 3.1.1 Commands to Implement

| Command | Response |
|---------|----------|
| `/status` | Current mode, uptime, last poll time, WS connection status, active subscriptions |
| `/positions` | Open positions with current P&L, entry price, current price |
| `/orders` | Resting orders with queue position, time in queue |
| `/balance` | Portfolio balance and available buying power |
| `/signals` | Last 5 signals with edge, confidence, direction |
| `/accuracy` | Brier score, hit rate, P&L summary for last 7/30 days |
| `/mode [mode]` | Show or change bot mode (with confirmation prompt for live modes) |
| `/pause` | Pause signal generation and order placement (keep data collection running) |
| `/resume` | Resume signal generation and order placement |

#### 3.1.2 Safety for Mode Changes

```
User: /mode live_auto
Bot:  ⚠️ You are about to enable LIVE AUTO TRADING.
      Current balance: $1,247.50
      Active positions: 3
      Model accuracy (30d): Brier 0.18, Hit rate 62%
      
      Type "CONFIRM LIVE" to proceed.
User: CONFIRM LIVE
Bot:  ✅ Mode changed to live_auto. Trading is now active.
```

**Acceptance criteria:**
- [ ] All commands listed above respond within 2 seconds
- [ ] Mode changes require explicit confirmation for any mode that places real orders
- [ ] `/pause` and `/resume` work without restarting the worker
- [ ] Commands are available 24/7 (handler runs alongside main loop)

---

### 3.2 Edge Decay Alerting

**New file:** `kalshi_pipeline/signals/edge_monitor.py`

After placing an order based on a signal, continue monitoring whether the edge still exists.

```python
async def monitor_edge_decay(
    open_positions: list[dict],
    current_signals: list[Signal],
):
    """
    Check if the edge that justified a position has decayed.
    Alert via Telegram if edge drops below threshold.
    """
    for position in open_positions:
        # Find current signal for same ticker
        current = next((s for s in current_signals if s.ticker == position["ticker"]), None)

        if current is None:
            # No signal generated for this ticker anymore — edge may have disappeared
            await send_alert(f"⚠️ No signal for {position['ticker']} — consider closing")
            continue

        if current.direction != position["direction"]:
            # Signal flipped! Model now says the opposite direction
            await send_alert(
                f"🔴 Signal FLIPPED for {position['ticker']}\n"
                f"Position: {position['direction']} at {position['entry_price']}¢\n"
                f"Model now says: {current.direction} with {current.edge_bps}bps edge\n"
                f"Consider closing immediately"
            )

        elif abs(current.edge_bps) < EDGE_DECAY_ALERT_THRESHOLD:
            # Edge has shrunk below alert threshold
            await send_alert(
                f"⚠️ Edge decayed for {position['ticker']}\n"
                f"Entry edge: {position['entry_edge_bps']}bps → Current: {current.edge_bps}bps\n"
                f"Consider closing if edge continues to decay"
            )
```

Run this check every signal generation cycle (every 30-60 seconds for BTC, every model update for weather).

**Acceptance criteria:**
- [ ] Alert sent when signal flips direction on an open position
- [ ] Alert sent when edge decays below configurable threshold
- [ ] Alert sent when no signal is generated for a ticker with an open position
- [ ] Alerts include actionable information (current price, suggested action)

---

### 3.3 Debug CLI Tool

**New file:** `kalshi_pipeline/cli.py`

A quick command-line tool to avoid ad-hoc SQL for common checks.

```bash
# Current state
python -m kalshi_pipeline.cli status
python -m kalshi_pipeline.cli positions
python -m kalshi_pipeline.cli balance

# Historical analysis
python -m kalshi_pipeline.cli accuracy --days 30
python -m kalshi_pipeline.cli signals --last 20
python -m kalshi_pipeline.cli trades --today

# Debug
python -m kalshi_pipeline.cli orderbook KXBTC15M-26FEB081830
python -m kalshi_pipeline.cli forecast --date 2026-02-09
python -m kalshi_pipeline.cli ws-status
```

Implementation: use `argparse` or `click`. Each subcommand queries the database or Kalshi API directly and prints formatted output to stdout.

**Acceptance criteria:**
- [ ] All subcommands listed above work
- [ ] Output is formatted for terminal readability (colors, tables)
- [ ] No dependency on the running worker process (reads from DB directly)

---

## Tier 4: Housekeeping (Do Anytime)

Quick wins that improve code quality and prevent future issues.

---

### 4.1 Remove `.DS_Store` from Repository

```bash
echo ".DS_Store" >> .gitignore
echo "*.pyc" >> .gitignore
echo "__pycache__/" >> .gitignore
echo ".env" >> .gitignore
git rm --cached .DS_Store
git rm --cached -r **/.DS_Store
git commit -m "chore: remove .DS_Store and update .gitignore"
```

### 4.2 Pin Dependency Versions

Your `requirements.txt` should pin exact versions to prevent breaking changes on deploy:

```
# Instead of:
requests
websockets
psycopg2-binary

# Use:
requests==2.31.0
websockets==12.0
psycopg2-binary==2.9.9
cryptography==42.0.0
python-telegram-bot==21.0
```

Run `pip freeze > requirements.txt` after confirming everything works, then manually remove unnecessary transitive dependencies.

### 4.3 Consider `pykalshi` for Transport Layer

The [`pykalshi`](https://github.com/arshka/pykalshi) library handles:
- RSA-PSS authentication with automatic header generation
- WebSocket feed management with typed message classes
- Automatic retry with exponential backoff on rate limits
- Local orderbook manager that applies WS deltas
- Pydantic models for type safety

You could replace your `kalshi_client.py` and most of the WS code in Tier 1 with pykalshi, and focus your custom code on signal generation and strategy logic. Evaluate the tradeoff: pykalshi saves development time but adds a dependency you don't control.

```bash
pip install pykalshi
```

```python
from pykalshi import KalshiClient, Feed, OrderbookManager

client = KalshiClient()  # reads from .env
manager = OrderbookManager()

async with Feed(client) as feed:
    await feed.subscribe_orderbook("KXBTC15M-26FEB081830")
    async for msg in feed:
        manager.apply(msg)
        book = manager.get("KXBTC15M-26FEB081830")
        # ... your signal logic here
```

### 4.4 Add Unit Tests

**New directory:** `tests/`

Priority test cases:

```
tests/
├── test_weather_signal.py
│   ├── test_bracket_probability_from_ensemble()
│   ├── test_edge_calculation()
│   ├── test_dst_measurement_window()
│   └── test_bracket_parsing_from_title()
├── test_btc_signal.py
│   ├── test_fair_value_with_all_sources()
│   ├── test_fair_value_with_partial_sources()
│   ├── test_confidence_degradation()
│   └── test_signal_direction()
├── test_risk.py
│   ├── test_kelly_fraction_positive_edge()
│   ├── test_kelly_fraction_negative_edge()
│   ├── test_position_sizing_caps()
│   └── test_quarter_kelly_scaling()
├── test_order_placement.py
│   ├── test_maker_price_inside_spread()
│   ├── test_maker_price_locked_spread()
│   ├── test_arbitrage_detection()
│   └── test_price_clamp_bounds()
└── test_config.py
    ├── test_profile_resolution()
    ├── test_mode_defaults()
    └── test_key_profile_isolation()
```

Use `pytest`. These tests should run without a database or API connection (mock external calls).

---

## Execution Timeline

| Week | Focus | Deliverables |
|------|-------|-------------|
| 1 | Tier 1.2 — Ensemble weather model | Multi-model ensemble collector, bracket probability calculator, weather schema updates |
| 2 | Tier 1.1 — WebSocket infrastructure | WSManager, KalshiFeed, BinanceFeed, async main loop |
| 2 | Tier 1.1 — Exchange feeds | Coinbase + Kraken WS feeds, weighted fair value |
| 3 | Tier 1.3 — Resolution tracking | CLI parser, resolution collector, accuracy schema, backtest report |
| 3 | Tier 2.2 — Confidence degradation | Replace quorum gate, weighted confidence scoring |
| 4 | Tier 2.1 — Order execution | Maker-only policy, arbitrage detection, queue monitoring |
| 4 | Tier 2.3 — Kelly sizing | Kelly calculator, position sizing integration |
| 5 | Tier 3.1 — Telegram commands | `/status`, `/positions`, `/accuracy`, `/mode` with confirmation |
| 5 | Tier 3.2 — Edge decay alerts | Monitor open positions, alert on edge decay/flip |
| 6 | Tier 3.3 + 4.x — CLI tool + housekeeping | Debug CLI, tests, dependency pinning, .DS_Store cleanup |

**Week 1 is the highest-leverage week.** The ensemble weather model is the core intellectual edge for KXHIGHNY. Everything else is execution improvement around that core signal.

**Data collection starts immediately.** Even while building Tier 1, your existing pipeline should keep collecting market snapshots and weather data. Every day of historical data makes backtesting more statistically robust.

---

## Decision Points

### When to go live (all must be true):

1. ✅ At least 100 resolved weather signals with accuracy metrics
2. ✅ Brier score < market-implied Brier score (your model outperforms)
3. ✅ Edge reliability > 55% (more than half of "edge" signals are profitable)
4. ✅ Paper trading P&L positive for 2+ consecutive weeks
5. ✅ All Tier 1 and Tier 2 items complete
6. ✅ Telegram `/status` and `/positions` commands working
7. ✅ Kelly-based position sizing validated on paper trades

### Starting capital recommendation:

- Start with $500–1,000 on weather markets only (lower frequency, easier to monitor)
- Add BTC 15-min after 1 week of live weather trading with positive P&L
- Scale up position sizes gradually as confidence in live execution grows
- Never risk more than 5% of bankroll on a single market
