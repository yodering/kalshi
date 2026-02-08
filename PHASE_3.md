# Kalshi Bot — Phase 3 Implementation Plan

## Context

This plan follows Implementation Report 2. The bot is operationally running with WS infrastructure, ensemble weather modeling, resolution tracking, Kelly sizing, Telegram commands, and demo-mode execution. This phase focuses on closing the gaps identified in review and incorporating the applicable ideas from the Polymarket arbitrage research.

Estimated total effort: ~25–35 hours across all tiers.

---

## Tier 1: Close the Critical Gaps (Do First)

These are things that are partially built but not yet delivering value. Finishing them is higher leverage than building anything new.

---

### 1.1 Promote WebSocket Data into Signal Inputs

**Problem:** The WS infrastructure is running but signal generation still reads from REST poll snapshots. For BTC 15-minute markets, this means signals are based on data that's `POLL_INTERVAL / 2` seconds stale on average. The entire point of building WS was to fix this.

**Goal:** BTC signal generation reads from live WS price buffers and Kalshi orderbook state, falling back to REST snapshots only when WS is disconnected.

#### 1.1.1 Create a Unified Price Provider

**New file:** `kalshi_pipeline/data/price_provider.py`

This module abstracts the data source so signal code doesn't care whether it's reading from WS or REST.

```python
class PriceProvider:
    """
    Unified interface for price data.
    Reads from WS buffers when available, falls back to REST snapshots.
    """

    def __init__(self, binance_feed, coinbase_feed, kraken_feed, kalshi_feed, db_pool):
        self._binance = binance_feed      # WS feed instance or None
        self._coinbase = coinbase_feed
        self._kraken = kraken_feed
        self._kalshi = kalshi_feed
        self._db = db_pool                # for REST fallback

    def get_btc_prices(self) -> dict[str, PriceSnapshot]:
        """
        Return latest BTC price from each available source.
        Prefers WS data. Falls back to most recent DB row if WS is stale (>5s old).
        """
        prices = {}
        for name, feed in [("binance", self._binance), ("coinbase", self._coinbase), ("kraken", self._kraken)]:
            if feed and feed.is_connected and feed.age_seconds < 5.0:
                prices[name] = PriceSnapshot(
                    price=feed.get_latest_price(),
                    timestamp=feed.last_update_time,
                    source="ws",
                )
            else:
                # Fall back to most recent crypto_spot_ticks row
                row = self._db.get_latest_spot_tick(name)
                if row and row.age_seconds < 30.0:
                    prices[name] = PriceSnapshot(
                        price=row.price,
                        timestamp=row.timestamp,
                        source="rest_fallback",
                    )
        return prices

    def get_kalshi_orderbook(self, ticker: str) -> Optional[Orderbook]:
        """
        Return local orderbook from WS state.
        Fall back to REST GET /markets/{ticker}/orderbook if WS is unavailable.
        """
        if self._kalshi and self._kalshi.has_orderbook(ticker):
            return self._kalshi.get_orderbook(ticker)
        return self._rest_fetch_orderbook(ticker)

    def get_btc_momentum(self, window_seconds: int = 300) -> Optional[float]:
        """
        Compute price momentum from WS trade buffer.
        Returns percentage change over window.
        Only available when Binance WS is connected (highest volume).
        """
        if self._binance and self._binance.is_connected:
            history = self._binance.get_price_history(window_seconds)
            if len(history) >= 2:
                return (history[-1] - history[0]) / history[0]
        return None
```

#### 1.1.2 Wire PriceProvider into BTC Signal Generation

**Modified file:** `kalshi_pipeline/signals/btc.py`

Replace direct DB reads with PriceProvider calls:

```python
# BEFORE (current)
def generate_btc_signal(db, market):
    spot_ticks = db.get_recent_spot_ticks(...)
    prices = {row.source: row.price for row in spot_ticks}
    # ... compute fair value from prices

# AFTER
def generate_btc_signal(price_provider, market):
    prices = price_provider.get_btc_prices()
    orderbook = price_provider.get_kalshi_orderbook(market["ticker"])
    momentum = price_provider.get_btc_momentum(window_seconds=300)
    # ... compute fair value using live data
```

The signal function signature changes. This means `pipeline.py` needs to instantiate `PriceProvider` and pass it to signal generation. The PriceProvider is created once at startup and holds references to the WS feed objects.

#### 1.1.3 Add Data Source Tagging to Signals

**Modified file:** `kalshi_pipeline/schema.sql`

Add a column to track what data source backed each signal:

```sql
ALTER TABLE signals ADD COLUMN IF NOT EXISTS data_source TEXT DEFAULT 'rest';
-- Values: 'ws', 'rest_fallback', 'mixed'
```

This lets you later analyze whether WS-sourced signals perform differently from REST-sourced signals, which validates the entire WS investment.

#### 1.1.4 Async Signal Loop in the WS Runtime

**Modified file:** `kalshi_pipeline/async_runtime.py`

Add a dedicated signal generation task that runs on a tighter loop than REST polling:

```python
async def signal_generation_loop(price_provider, pipeline, interval_seconds=5):
    """
    Generate signals every N seconds using live WS data.
    Separate from REST poll loop.
    """
    while not shutdown_event.is_set():
        try:
            # Only for BTC 15-min markets (weather doesn't need sub-minute signals)
            active_btc_markets = await get_active_btc_markets()
            for market in active_btc_markets:
                signal = generate_btc_signal(price_provider, market)
                if signal and signal.is_actionable:
                    await persist_signal(signal)
                    await maybe_execute(signal)
        except Exception as e:
            log.error(f"Signal loop error: {e}")
        await asyncio.sleep(interval_seconds)
```

For weather markets, keep the existing cycle-driven signal generation (once per model update, roughly hourly). Only BTC needs the fast loop.

**Acceptance criteria:**
- [ ] BTC signals tagged with `data_source='ws'` when WS feeds are connected
- [ ] Signal latency measurably improved (log time from price event to signal generation)
- [ ] Fallback to REST works transparently when WS disconnects
- [ ] No change to weather signal path (still cycle-driven)
- [ ] PriceProvider unit tested with mock feeds

---

### 1.2 Validate Ensemble Weather Model Against Historical Data

**Problem:** The ensemble collector and bracket probability calculator are built and running, but there's no evidence yet that the model's bracket probabilities are better-calibrated than the market's implied probabilities. Without this validation, you don't know if the weather signal has real edge.

**Goal:** Produce a definitive answer to "does my ensemble model outperform the market?" using at least 30 days of historical data.

#### 1.2.1 Historical Backtest Dataset Builder

**New file:** `kalshi_pipeline/analysis/weather_backtest.py`

This script builds the backtest dataset by combining three sources:

```python
def build_weather_backtest_dataset(start_date: str, end_date: str) -> pd.DataFrame:
    """
    For each historical date in range:
    1. Pull ensemble forecast from Open-Meteo historical archive
    2. Compute what bracket probabilities would have been
    3. Pull actual NWS resolution (actual daily high)
    4. Pull Kalshi market prices at various times during the day
    5. Combine into one row per bracket per date

    Returns DataFrame with columns:
        date, ticker, bracket_low, bracket_high,
        model_prob,          # ensemble-derived P(actual temp in this bracket)
        market_prob_open,    # Kalshi implied prob at market open
        market_prob_midday,  # Kalshi implied prob at noon
        market_prob_close,   # Kalshi implied prob at market close
        actual_outcome,      # 1 if actual temp fell in this bracket, 0 otherwise
        actual_temp_f        # actual daily high from NWS
    """
```

Data sources for the backtest:

**Open-Meteo Historical Weather API** (for reconstructing what the ensemble would have predicted):
```
GET https://archive-api.open-meteo.com/v1/archive
  ?latitude=40.7829&longitude=-73.9654
  &start_date=2025-12-01&end_date=2026-01-31
  &hourly=temperature_2m
  &temperature_unit=fahrenheit
  &timezone=America/New_York
```

Note: The archive API gives actual historical observations, not forecasts. For a true backtest of forecast skill, you need historical forecast data. Open-Meteo provides this via their "previous runs" feature on the forecast endpoints if you specify `&past_days=N`. However, this is limited to recent days.

**Alternative approach (more practical):** Use the data you've already been collecting. Your `weather_ensemble_forecasts` table has been storing ensemble member predictions since you deployed the collector. Your `market_snapshots` table has Kalshi prices. Your `market_resolutions` table has actual outcomes. Join them:

```sql
SELECT
    wbp.target_date,
    wbp.ticker,
    wbp.bracket_low,
    wbp.bracket_high,
    wbp.model_prob,
    wbp.market_prob,
    wbp.edge,
    CASE WHEN mr.result = 'yes' THEN 1 ELSE 0 END AS actual_outcome,
    mr.actual_value AS actual_temp_f
FROM weather_bracket_probs wbp
JOIN market_resolutions mr ON mr.ticker = wbp.ticker
WHERE wbp.target_date BETWEEN $1 AND $2
ORDER BY wbp.target_date, wbp.bracket_low;
```

#### 1.2.2 Calibration and Scoring Metrics

**Extended in:** `kalshi_pipeline/analysis/accuracy_report.py`

Add weather-specific metrics on top of the general accuracy report:

```python
def weather_calibration_report(start_date, end_date) -> dict:
    """
    Returns:
        model_brier:       Brier score of ensemble bracket probabilities
        market_brier:      Brier score of market-implied bracket probabilities
        brier_advantage:   market_brier - model_brier (positive = model is better)
        model_log_loss:    Log loss of model probabilities
        market_log_loss:   Log loss of market probabilities
        calibration_table: Bucketed predicted prob vs actual frequency
            e.g., "When model says 20-30%, outcome actually occurs X% of time"
        edge_hit_rate:     Of brackets where model_prob > market_prob, % that resolved YES
        edge_miss_rate:    Of brackets where model_prob > market_prob, % that resolved NO
        profit_simulation: Simulated P&L if you had bought every bracket where edge > 5%
        n_brackets:        Total bracket-days in sample
    """
```

The critical number is `brier_advantage`. If it's positive, your model is better than the market. If it's negative or near zero, your weather signal doesn't have edge regardless of how sophisticated the infrastructure is.

```python
def compute_brier_score(predictions: list[tuple[float, int]]) -> float:
    """
    Brier score = mean of (predicted_prob - actual_outcome)^2
    Lower is better. Range [0, 1].
    
    predictions: list of (predicted_probability, actual_outcome_0_or_1)
    """
    return sum((p - o) ** 2 for p, o in predictions) / len(predictions)
```

#### 1.2.3 Calibration Curve Visualization

**New file:** `kalshi_pipeline/analysis/calibration_plot.py`

Generate a calibration plot that you can view in Telegram or export as an image:

```python
def generate_calibration_data(predictions: list[tuple[float, int]], n_bins=10) -> list[dict]:
    """
    Bucket predictions into bins and compute actual frequency per bin.

    Returns list of:
        {"bin_center": 0.25, "predicted_avg": 0.23, "actual_freq": 0.28, "count": 45}

    Perfect calibration: predicted_avg == actual_freq for every bin.
    """
    bins = [[] for _ in range(n_bins)]
    for prob, outcome in predictions:
        idx = min(int(prob * n_bins), n_bins - 1)
        bins[idx].append((prob, outcome))

    results = []
    for i, bucket in enumerate(bins):
        if not bucket:
            continue
        probs, outcomes = zip(*bucket)
        results.append({
            "bin_center": (i + 0.5) / n_bins,
            "predicted_avg": sum(probs) / len(probs),
            "actual_freq": sum(outcomes) / len(outcomes),
            "count": len(bucket),
        })
    return results
```

Add a Telegram command that sends the calibration summary:

```
/calibration

📊 Weather Model Calibration (30 days, 186 bracket-days)

Model Brier:   0.142
Market Brier:  0.168
Advantage:     +0.026 ✅ (model is better)

Predicted → Actual frequency:
 0-10%:  7.2%  (n=42) ✅
10-20%: 14.1%  (n=38) ✅
20-30%: 31.5%  (n=29) ⚠️ slightly overconfident
30-50%: 38.9%  (n=44) ✅
50-70%: 62.3%  (n=21) ✅
70%+:   78.6%  (n=12) ✅
```

#### 1.2.4 Decision Gate

Define a concrete rule: **do not enable live weather trading until:**

1. At least 30 resolved days of ensemble predictions (minimum ~180 bracket-days)
2. `model_brier < market_brier` (your model outperforms)
3. Simulated profit (buying every bracket where edge > 5%) is positive after fees
4. No single bracket bin is miscalibrated by more than 15 percentage points

Store these gate conditions in config so you can check them programmatically:

```python
WEATHER_LIVE_GATES = {
    "min_resolved_days": 30,
    "min_brier_advantage": 0.005,  # model must be at least 0.5% better
    "min_sim_profit_cents": 0,     # must be positive
    "max_calibration_error": 0.15, # no bin off by more than 15%
}
```

**Acceptance criteria:**
- [ ] Backtest query joins ensemble predictions with resolutions and market prices
- [ ] Brier score computed for both model and market, with advantage metric
- [ ] Calibration curve computed and available via `/calibration` Telegram command
- [ ] Profit simulation computed assuming 1-contract maker orders on every edge > 5% signal
- [ ] Decision gate defined and checkable programmatically
- [ ] If model doesn't pass gates after 30 days, investigate and recalibrate before going live

---

### 1.3 Intra-Event Bracket Arbitrage Scanner

**Why:** This is the highest-value idea from the Polymarket article that directly applies to Kalshi. For mutually exclusive bracket events (KXHIGHNY), if you can buy YES on every bracket for less than $1.00 total, or buy NO on every bracket for less than $(N-1).00, you lock in risk-free profit. This check is cheap to compute and the payoff is guaranteed.

#### 1.3.1 The Scanner

**New file:** `kalshi_pipeline/signals/bracket_arb.py`

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class BracketArbOpportunity:
    event_ticker: str
    arb_type: str          # 'all_yes' or 'all_no'
    legs: list[dict]       # [{"ticker": ..., "side": ..., "price": ..., "depth": ...}, ...]
    cost_cents: int        # total cost to buy all legs
    payout_cents: int      # guaranteed payout (100 for all_yes, (N-1)*100 for all_no)
    profit_cents: int      # payout - cost
    max_sets: int          # limited by minimum depth across all legs
    total_profit_cents: int  # profit_cents * max_sets
    profit_after_fees: int   # accounting for taker fees on each leg


def scan_bracket_arbitrage(
    event_ticker: str,
    bracket_tickers: list[str],
    orderbooks: dict[str, dict],
    fee_calculator,
) -> Optional[BracketArbOpportunity]:
    """
    Check for arbitrage across all brackets in a mutually exclusive event.

    Two checks:
    1. All-YES: Buy YES on every bracket. Guaranteed one pays $1.00.
       Arbitrage if total cost < $1.00.
    2. All-NO: Buy NO on every bracket. Guaranteed (N-1) pay $1.00.
       Arbitrage if total cost < (N-1) * $1.00.

    Returns the best opportunity found, or None.
    """
    n = len(bracket_tickers)
    if n < 2:
        return None

    # --- Check 1: All-YES arbitrage ---
    yes_legs = []
    yes_total = 0
    yes_min_depth = float('inf')

    for ticker in bracket_tickers:
        book = orderbooks.get(ticker)
        if not book or not book.get("no"):
            # Can't compute YES ask without NO bids
            break

        # YES ask = 100 - best NO bid
        no_bids = sorted(book["no"], key=lambda x: x[0], reverse=True)
        best_no_bid = no_bids[0][0]
        yes_ask = 100 - best_no_bid
        depth_at_ask = sum(q for p, q in no_bids if p == best_no_bid)

        yes_legs.append({
            "ticker": ticker,
            "side": "yes",
            "price_cents": yes_ask,
            "depth": depth_at_ask,
        })
        yes_total += yes_ask
        yes_min_depth = min(yes_min_depth, depth_at_ask)
    else:
        # Loop completed without break — all brackets have data
        if yes_total < 100:
            # Compute fees for each leg
            total_fees = sum(
                fee_calculator.taker_fee(leg["price_cents"])
                for leg in yes_legs
            )
            profit_after_fees = (100 - yes_total) - total_fees
            if profit_after_fees > 0:
                return BracketArbOpportunity(
                    event_ticker=event_ticker,
                    arb_type="all_yes",
                    legs=yes_legs,
                    cost_cents=yes_total,
                    payout_cents=100,
                    profit_cents=100 - yes_total,
                    max_sets=int(yes_min_depth),
                    total_profit_cents=(100 - yes_total) * int(yes_min_depth),
                    profit_after_fees=int(profit_after_fees * yes_min_depth),
                )

    # --- Check 2: All-NO arbitrage ---
    no_legs = []
    no_total = 0
    no_min_depth = float('inf')

    for ticker in bracket_tickers:
        book = orderbooks.get(ticker)
        if not book or not book.get("yes"):
            break

        # NO ask = 100 - best YES bid
        yes_bids = sorted(book["yes"], key=lambda x: x[0], reverse=True)
        best_yes_bid = yes_bids[0][0]
        no_ask = 100 - best_yes_bid
        depth_at_ask = sum(q for p, q in yes_bids if p == best_yes_bid)

        no_legs.append({
            "ticker": ticker,
            "side": "no",
            "price_cents": no_ask,
            "depth": depth_at_ask,
        })
        no_total += no_ask
        no_min_depth = min(no_min_depth, depth_at_ask)
    else:
        guaranteed_payout = (n - 1) * 100
        if no_total < guaranteed_payout:
            total_fees = sum(
                fee_calculator.taker_fee(leg["price_cents"])
                for leg in no_legs
            )
            profit_after_fees = (guaranteed_payout - no_total) - total_fees
            if profit_after_fees > 0:
                return BracketArbOpportunity(
                    event_ticker=event_ticker,
                    arb_type="all_no",
                    legs=no_legs,
                    cost_cents=no_total,
                    payout_cents=guaranteed_payout,
                    profit_cents=guaranteed_payout - no_total,
                    max_sets=int(no_min_depth),
                    total_profit_cents=(guaranteed_payout - no_total) * int(no_min_depth),
                    profit_after_fees=int(profit_after_fees * no_min_depth),
                )

    return None


class KalshiFeeCalculator:
    """
    Kalshi taker fee: $0.07 * P * (1-P) per contract, rounded up to nearest cent.
    Maker fee: $0.00.
    """

    @staticmethod
    def taker_fee(price_cents: int) -> int:
        p = price_cents / 100.0
        fee = 0.07 * p * (1 - p)
        return max(1, int(fee * 100 + 0.999))  # round up to nearest cent

    @staticmethod
    def maker_fee(price_cents: int) -> int:
        return 0
```

#### 1.3.2 Integration into Pipeline

**Modified file:** `kalshi_pipeline/pipeline.py`

Run the scanner every cycle for every active mutually exclusive event:

```python
async def check_bracket_arbitrage(self):
    """Run bracket arbitrage scanner on all active weather events."""
    events = await self.get_active_events("KXHIGHNY")
    for event in events:
        bracket_tickers = [m["ticker"] for m in event["markets"]]

        # Get orderbooks for all brackets
        orderbooks = {}
        for ticker in bracket_tickers:
            book = self.price_provider.get_kalshi_orderbook(ticker)
            if book:
                orderbooks[ticker] = book

        if len(orderbooks) < len(bracket_tickers):
            continue  # missing data, skip

        opp = scan_bracket_arbitrage(
            event["event_ticker"],
            bracket_tickers,
            orderbooks,
            KalshiFeeCalculator(),
        )

        if opp:
            await self.handle_arbitrage(opp)
```

#### 1.3.3 Arbitrage Execution Path

**Modified file:** `kalshi_pipeline/paper_trading.py`

Arbitrage opportunities get special handling — they bypass the normal signal threshold checks because the edge is structural, not probabilistic:

```python
async def handle_arbitrage(self, opp: BracketArbOpportunity):
    """Execute bracket arbitrage opportunity."""
    # Log and alert
    log.info(f"ARBITRAGE DETECTED: {opp.event_ticker} "
             f"type={opp.arb_type} profit={opp.profit_after_fees}¢ "
             f"x{opp.max_sets} sets")

    await self.telegram.send_alert(
        f"🎯 ARBITRAGE: {opp.event_ticker}\n"
        f"Type: {opp.arb_type}\n"
        f"Profit: {opp.profit_after_fees}¢/set × {opp.max_sets} sets\n"
        f"Total: ${opp.profit_after_fees * opp.max_sets / 100:.2f}\n"
        f"Legs: {len(opp.legs)}"
    )

    # In demo/paper mode: record but don't execute
    # In live mode: execute all legs as IOC or limit orders
    if self.mode in ("live_safe", "live_auto"):
        for leg in opp.legs:
            await self.place_order(
                ticker=leg["ticker"],
                side=leg["side"],
                price_cents=leg["price_cents"],
                count=opp.max_sets,
                order_type="limit",  # maker preferred, but taker acceptable for arb
            )
```

#### 1.3.4 Arbitrage Tracking Table

**Modified file:** `kalshi_pipeline/schema.sql`

```sql
CREATE TABLE IF NOT EXISTS bracket_arb_opportunities (
    id SERIAL PRIMARY KEY,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_ticker TEXT NOT NULL,
    arb_type TEXT NOT NULL,            -- 'all_yes' or 'all_no'
    n_brackets INT NOT NULL,
    cost_cents INT NOT NULL,
    payout_cents INT NOT NULL,
    profit_cents INT NOT NULL,
    profit_after_fees INT NOT NULL,
    max_sets INT NOT NULL,
    legs JSONB NOT NULL,               -- full leg details
    executed BOOLEAN DEFAULT FALSE,
    execution_result JSONB             -- order IDs, fill status, actual P&L
);
```

**Acceptance criteria:**
- [ ] Scanner runs every cycle for all active KXHIGHNY events
- [ ] Both all-YES and all-NO arbitrage types detected
- [ ] Fee calculation included (only profitable-after-fees opportunities flagged)
- [ ] Depth-limited (max_sets constrained by minimum liquidity across legs)
- [ ] Telegram alert sent immediately on detection
- [ ] Opportunity persisted to DB for historical analysis
- [ ] In live mode, all legs submitted as orders
- [ ] Scanner also works for any future mutually exclusive event series

---

## Tier 2: Execution Hardening (Do Second)

These improvements prevent real-money losses from bugs, race conditions, and edge cases.

---

### 2.1 Liquidity-Aware Edge Sizing

**Problem:** The current signal computes edge as `model_prob - market_implied_prob` using best bid/ask. But if there are only 2 contracts at the best ask and you want to buy 10, your actual fill price is much worse. The edge might evaporate entirely.

**Goal:** Every signal includes an expected fill price (VWAP) at the target order size, and edge is computed against VWAP, not best bid/ask.

#### 2.1.1 VWAP Calculator

**New file:** `kalshi_pipeline/orderbook_utils.py`

```python
def compute_vwap(
    price_levels: list[tuple[int, int]],
    target_qty: int,
    ascending: bool = True,
) -> Optional[tuple[float, int]]:
    """
    Compute volume-weighted average price to fill target_qty contracts.

    Args:
        price_levels: [(price_cents, quantity), ...] from orderbook
        target_qty: number of contracts to fill
        ascending: True if buying (walk up the ask), False if selling (walk down the bid)

    Returns:
        (vwap_cents, fillable_qty) or None if no liquidity
    """
    sorted_levels = sorted(price_levels, key=lambda x: x[0], reverse=not ascending)

    filled = 0
    total_cost = 0
    for price, qty in sorted_levels:
        can_fill = min(qty, target_qty - filled)
        total_cost += price * can_fill
        filled += can_fill
        if filled >= target_qty:
            break

    if filled == 0:
        return None
    return (total_cost / filled, filled)


def effective_yes_ask_vwap(orderbook: dict, qty: int) -> Optional[tuple[float, int]]:
    """
    Compute VWAP for buying YES contracts.
    YES asks are derived from NO bids: YES ask at price (100 - no_bid_price).
    """
    if not orderbook.get("no"):
        return None

    # Convert NO bids to YES ask levels
    yes_ask_levels = [(100 - p, q) for p, q in orderbook["no"]]
    return compute_vwap(yes_ask_levels, qty, ascending=True)


def effective_no_ask_vwap(orderbook: dict, qty: int) -> Optional[tuple[float, int]]:
    """Compute VWAP for buying NO contracts."""
    if not orderbook.get("yes"):
        return None
    no_ask_levels = [(100 - p, q) for p, q in orderbook["yes"]]
    return compute_vwap(no_ask_levels, qty, ascending=True)
```

#### 2.1.2 Use VWAP in Signal Edge Calculation

**Modified file:** `kalshi_pipeline/signals/weather.py` and `btc.py`

```python
# BEFORE
market_prob = snap["yes_ask"] / 100.0
edge = model_prob - market_prob

# AFTER
target_qty = estimate_order_size(...)  # from Kelly sizing
vwap_result = effective_yes_ask_vwap(orderbook, target_qty)
if vwap_result is None:
    continue  # no liquidity, skip
vwap_cents, fillable = vwap_result
market_prob = vwap_cents / 100.0
edge = model_prob - market_prob

# Also record the liquidity context
signal.vwap_cents = vwap_cents
signal.fillable_qty = fillable
signal.liquidity_sufficient = (fillable >= target_qty)
```

#### 2.1.3 Schema Addition

```sql
ALTER TABLE signals ADD COLUMN IF NOT EXISTS vwap_cents NUMERIC;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS fillable_qty INT;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS liquidity_sufficient BOOLEAN;
```

**Acceptance criteria:**
- [ ] VWAP computed for every signal at the target order size
- [ ] Edge calculation uses VWAP, not best bid/ask
- [ ] Signals on illiquid brackets (fillable_qty < target) flagged and potentially skipped
- [ ] VWAP persisted for post-hoc analysis of execution quality

---

### 2.2 Execution Probability in Kelly Sizing

**Problem:** Current Kelly sizing doesn't account for fill probability. If your maker orders only fill 40% of the time on thin weather brackets, your expected edge is 40% of theoretical. Oversizing on low-fill-probability markets wastes capital that could be deployed elsewhere.

**Modified file:** `kalshi_pipeline/risk.py`

#### 2.2.1 Historical Fill Rate Estimator

```python
def estimate_fill_probability(
    db,
    ticker_prefix: str,       # e.g., "KXHIGHNY" or "KXBTC15M"
    price_range: tuple = None, # e.g., (10, 30) for 10-30¢ contracts
    lookback_days: int = 14,
) -> float:
    """
    Estimate fill probability from historical order data.

    Looks at paper_trade_orders + paper_trade_order_events for:
    - Orders placed (maker limit orders)
    - Orders filled (status='filled' or 'partially_filled')
    - Compute fill_rate = filled / placed

    Returns fill probability in [0, 1]. Defaults to 0.5 if insufficient data.
    """
    query = """
        SELECT
            COUNT(*) AS total_orders,
            COUNT(*) FILTER (
                WHERE status IN ('filled', 'partially_filled')
            ) AS filled_orders
        FROM paper_trade_orders
        WHERE ticker LIKE $1 || '%'
          AND created_at > NOW() - INTERVAL '%s days'
    """
    # ... execute and return filled/total, or 0.5 if < 20 historical orders
```

#### 2.2.2 Adjusted Kelly Integration

```python
def compute_order_size(signal, bankroll, current_exposure, db):
    raw_kelly = kelly_fraction(signal.model_prob, signal.market_price_cents, signal.direction)

    # Adjust for fill probability
    fill_prob = estimate_fill_probability(
        db,
        ticker_prefix=signal.series_ticker,
        price_range=signal.price_range,
    )
    adjusted_kelly = raw_kelly * fill_prob

    # Adjust for confidence
    target_dollars = bankroll * adjusted_kelly * KELLY_FRACTION * signal.confidence

    # Apply caps (unchanged)
    target_dollars = min(target_dollars, MAX_POSITION_DOLLARS)
    target_dollars = min(target_dollars, MAX_PORTFOLIO_EXPOSURE - current_exposure)

    price_per_contract = signal.market_price_cents / 100.0
    return max(int(target_dollars / price_per_contract), 0)
```

**Acceptance criteria:**
- [ ] Fill probability estimated from historical order data
- [ ] Kelly fraction scaled by fill probability
- [ ] Orders on low-fill-rate markets (e.g., illiquid weather brackets) are smaller
- [ ] Default fill probability (0.5) used when insufficient historical data
- [ ] Fill probability logged with each order for analysis

---

### 2.3 Queue Repricing Circuit Breaker

**Problem:** The cancel-and-resubmit flow for deep-queue orders could loop indefinitely if the market keeps moving away from you.

**Modified file:** `kalshi_pipeline/paper_trading.py` or `kalshi_pipeline/order_utils.py`

```python
# Track repricing history per ticker
_reprice_counts: dict[str, list[float]] = {}  # ticker -> [timestamps]

MAX_REPRICES_PER_WINDOW = 3
REPRICE_WINDOW_SECONDS = 900  # 15 minutes
REPRICE_COOLDOWN_SECONDS = 60  # minimum time between reprices

def should_reprice(ticker: str) -> bool:
    """Check if repricing is allowed for this ticker."""
    now = time.time()
    history = _reprice_counts.get(ticker, [])

    # Clean old entries
    history = [t for t in history if now - t < REPRICE_WINDOW_SECONDS]
    _reprice_counts[ticker] = history

    if len(history) >= MAX_REPRICES_PER_WINDOW:
        return False

    if history and (now - history[-1]) < REPRICE_COOLDOWN_SECONDS:
        return False

    return True

def record_reprice(ticker: str):
    """Record that a reprice happened."""
    _reprice_counts.setdefault(ticker, []).append(time.time())
```

Wire this into the queue monitoring flow:

```python
async def maybe_reprice_order(order, queue_position):
    if queue_position <= MAX_QUEUE_DEPTH:
        return  # position is fine

    if not should_reprice(order["ticker"]):
        log.info(f"Reprice blocked for {order['ticker']}: circuit breaker active")
        return

    # Proceed with cancel + re-evaluate + resubmit
    await cancel_order(order["order_id"])
    record_reprice(order["ticker"])
    # ... re-evaluate and resubmit
```

**Acceptance criteria:**
- [ ] Maximum 3 reprices per ticker per 15-minute window
- [ ] Minimum 60 seconds between reprices on same ticker
- [ ] Circuit breaker state logged for debugging
- [ ] After circuit breaker triggers, order stays at current queue position (no action)

---

### 2.4 Critical Unit Tests for Money-Path Code

**New/extended files in `tests/`:**

These test the functions where a bug directly causes financial loss.

```
tests/
├── test_bracket_arb.py
│   ├── test_all_yes_arbitrage_detected()        # sum < 100
│   ├── test_all_yes_no_arbitrage()              # sum >= 100
│   ├── test_all_no_arbitrage_detected()         # sum < (N-1)*100
│   ├── test_fee_eliminates_arb()                # arb exists pre-fee but not post-fee
│   ├── test_depth_limits_max_sets()             # one bracket has 2 contracts, others 100
│   ├── test_missing_orderbook_data_returns_none()
│   └── test_single_bracket_returns_none()
│
├── test_orderbook_utils.py
│   ├── test_vwap_single_level()
│   ├── test_vwap_multiple_levels()
│   ├── test_vwap_insufficient_liquidity()
│   ├── test_yes_ask_from_no_bids()              # Kalshi's inverted representation
│   └── test_no_ask_from_yes_bids()
│
├── test_order_pricing.py
│   ├── test_maker_price_normal_spread()         # price inside spread
│   ├── test_maker_price_locked_spread()         # spread = 1¢
│   ├── test_maker_price_no_bids()               # empty book
│   └── test_maker_price_wide_spread()           # 20¢+ spread
│
├── test_risk.py
│   ├── test_kelly_positive_edge()
│   ├── test_kelly_negative_edge_returns_zero()
│   ├── test_kelly_edge_near_zero()
│   ├── test_sizing_respects_max_position()
│   ├── test_sizing_respects_portfolio_cap()
│   ├── test_adjusted_kelly_with_fill_probability()
│   └── test_sizing_with_zero_bankroll()
│
├── test_fee_calculator.py
│   ├── test_taker_fee_at_50_cents()             # max fee point
│   ├── test_taker_fee_at_5_cents()
│   ├── test_taker_fee_at_95_cents()
│   ├── test_taker_fee_rounds_up()
│   └── test_maker_fee_always_zero()
│
└── test_calibration.py
    ├── test_brier_score_perfect_predictions()    # should be 0.0
    ├── test_brier_score_worst_predictions()      # should be 1.0
    ├── test_calibration_bins_sum_correctly()
    └── test_empty_predictions_handled()
```

Each test should use only mock/synthetic data — no DB, no API calls.

**Acceptance criteria:**
- [ ] All tests pass: `python -m pytest tests/ -v`
- [ ] Coverage on bracket_arb, orderbook_utils, risk, fee_calculator, and calibration modules > 90%
- [ ] Tests run in CI (GitHub Actions) on every push
- [ ] No test takes longer than 1 second

---

## Tier 3: Operational Improvements (Do Third)

These make the bot easier to operate day-to-day.

---

### 3.1 Add `/calibration` Telegram Command

**Modified file:** `kalshi_pipeline/notifications.py`

Add the weather calibration report to the Telegram command handler:

```python
async def handle_calibration_command(self, args):
    days = int(args[0]) if args else 30
    report = await generate_weather_calibration(self.db, days)

    if report["n_brackets"] < 30:
        await self.send(
            f"📊 Insufficient data: {report['n_brackets']} bracket-days "
            f"(need 30+). Keep collecting."
        )
        return

    gates_passed = check_live_gates(report)

    msg = (
        f"📊 Weather Calibration ({days}d, {report['n_brackets']} brackets)\n\n"
        f"Model Brier:  {report['model_brier']:.4f}\n"
        f"Market Brier: {report['market_brier']:.4f}\n"
        f"Advantage:    {report['brier_advantage']:+.4f} "
        f"{'✅' if report['brier_advantage'] > 0 else '❌'}\n\n"
        f"Edge hit rate: {report['edge_hit_rate']:.1%}\n"
        f"Sim P&L:       {report['sim_pnl_cents']/100:+.2f}\n\n"
        f"Live gates: {'ALL PASSED ✅' if all(gates_passed.values()) else 'NOT READY ❌'}\n"
    )
    for gate, passed in gates_passed.items():
        msg += f"  {'✅' if passed else '❌'} {gate}\n"

    await self.send(msg)
```

### 3.2 Add `/arb` Telegram Command

Show recent arbitrage opportunities and whether they were executed:

```python
async def handle_arb_command(self, args):
    days = int(args[0]) if args else 7
    opps = await self.db.get_recent_arb_opportunities(days)

    if not opps:
        await self.send(f"No bracket arbitrage detected in last {days} days.")
        return

    msg = f"🎯 Bracket Arbitrage ({days}d): {len(opps)} opportunities\n\n"
    for opp in opps[-5:]:  # last 5
        msg += (
            f"{opp['event_ticker']} ({opp['arb_type']})\n"
            f"  Profit: {opp['profit_after_fees']}¢/set × {opp['max_sets']}\n"
            f"  Executed: {'✅' if opp['executed'] else '❌'}\n"
            f"  {opp['detected_at'].strftime('%m/%d %H:%M')}\n\n"
        )
    await self.send(msg)
```

### 3.3 GitHub Actions CI

**New file:** `.github/workflows/test.yml`

```yaml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: pip install pytest pytest-cov
      - run: python -m pytest tests/ -v --cov=kalshi_pipeline --cov-report=term-missing
```

This catches regressions before they hit Railway.

---

## Execution Timeline

| Week | Focus | Deliverables |
|------|-------|-------------|
| 1 | 1.1 — WS → signal integration | PriceProvider, async signal loop, data_source tagging |
| 1 | 1.3 — Bracket arb scanner | Scanner, pipeline integration, arb tracking table |
| 2 | 1.2 — Weather model validation | Backtest query, Brier scoring, calibration report, decision gates |
| 2 | 2.4 — Unit tests | All money-path tests, CI setup |
| 3 | 2.1 — VWAP edge sizing | VWAP calculator, signal integration, schema |
| 3 | 2.2 — Fill probability in Kelly | Historical fill rate estimator, adjusted Kelly |
| 3 | 2.3 — Queue circuit breaker | Reprice limiter, cooldown tracking |
| 4 | 3.x — Operational | `/calibration`, `/arb` commands, GitHub Actions CI |

---

## Decision Points

### After Week 2: Weather go/no-go

Review the calibration report. If `brier_advantage > 0.005` and simulated P&L is positive after fees, begin planning live weather trading with conservative sizing ($5-10 per bracket, max $50/day).

If the model doesn't pass gates: investigate. Possible causes and fixes:
- **Ensemble spread too wide** → weight HRRR more heavily for same-day forecasts (higher resolution, hourly updates)
- **Bracket parsing errors** → check that bracket bounds match what Kalshi actually uses
- **NWS measurement window wrong** → verify DST handling against actual CLI reports
- **Market is already efficient** → the weather edge might not exist. Redirect effort to BTC.

### After Week 3: BTC signal quality check

Compare WS-sourced BTC signals against REST-sourced signals from before the migration. Key metrics:
- Signal latency (time from price movement to signal generation)
- Edge accuracy (did WS signals have better hit rates?)
- Fill rates (did faster signals translate to better fills?)

If WS signals aren't measurably better, the added complexity isn't justified and you should simplify back to REST-only for BTC.

### After Week 4: Live readiness assessment

All of the following must be true before enabling `live_auto`:
1. Weather calibration gates passed (if pursuing weather)
2. Bracket arb scanner running without false positives for 2+ weeks
3. Paper trading P&L positive for 2+ consecutive weeks
4. All Tier 2 items complete (VWAP sizing, fill-prob Kelly, queue circuit breaker)
5. Unit test suite green with >90% coverage on money-path code
6. At least one successful Telegram `/mode live_auto` + `CONFIRM LIVE` dry run on demo
