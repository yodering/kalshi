CREATE TABLE IF NOT EXISTS markets (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    close_time TIMESTAMPTZ NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id BIGSERIAL PRIMARY KEY,
    market_id BIGINT NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    snapshot_ts TIMESTAMPTZ NOT NULL,
    yes_price DOUBLE PRECISION NULL,
    no_price DOUBLE PRECISION NULL,
    volume DOUBLE PRECISION NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market_id, snapshot_ts)
);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_market_id
ON market_snapshots (market_id);

CREATE INDEX IF NOT EXISTS idx_market_snapshots_snapshot_ts
ON market_snapshots (snapshot_ts DESC);

CREATE TABLE IF NOT EXISTS weather_ensemble_samples (
    id BIGSERIAL PRIMARY KEY,
    collected_at TIMESTAMPTZ NOT NULL,
    target_date DATE NOT NULL,
    model TEXT NOT NULL,
    member TEXT NOT NULL,
    max_temp_f DOUBLE PRECISION NOT NULL,
    source TEXT NOT NULL DEFAULT 'open-meteo',
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (collected_at, target_date, model, member)
);

CREATE INDEX IF NOT EXISTS idx_weather_ensemble_target_date
ON weather_ensemble_samples (target_date, collected_at DESC);

CREATE TABLE IF NOT EXISTS weather_ensemble_forecasts (
    id BIGSERIAL PRIMARY KEY,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_date DATE NOT NULL,
    model TEXT NOT NULL,
    member_index INTEGER NOT NULL,
    predicted_max_f DOUBLE PRECISION NOT NULL,
    forecast_hour INTEGER NULL,
    UNIQUE (collected_at, target_date, model, member_index)
);

CREATE INDEX IF NOT EXISTS idx_weather_ensemble_forecasts_target_date
ON weather_ensemble_forecasts (target_date, collected_at DESC);

CREATE TABLE IF NOT EXISTS weather_bracket_probs (
    id BIGSERIAL PRIMARY KEY,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    target_date DATE NOT NULL,
    ticker TEXT NOT NULL,
    bracket_low DOUBLE PRECISION NULL,
    bracket_high DOUBLE PRECISION NULL,
    model_prob DOUBLE PRECISION NOT NULL,
    market_prob DOUBLE PRECISION NULL,
    edge DOUBLE PRECISION NULL,
    ensemble_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (computed_at, ticker)
);

CREATE INDEX IF NOT EXISTS idx_weather_bracket_probs_target_date
ON weather_bracket_probs (target_date, computed_at DESC);

CREATE TABLE IF NOT EXISTS crypto_spot_ticks (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    price_usd DOUBLE PRECISION NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (ts, source, symbol)
);

CREATE INDEX IF NOT EXISTS idx_crypto_spot_ticks_symbol_ts
ON crypto_spot_ticks (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    signal_type TEXT NOT NULL,
    market_ticker TEXT NULL,
    direction TEXT NOT NULL,
    model_probability DOUBLE PRECISION NULL,
    market_probability DOUBLE PRECISION NULL,
    edge_bps DOUBLE PRECISION NULL,
    confidence DOUBLE PRECISION NULL,
    data_source TEXT NOT NULL DEFAULT 'rest',
    vwap_cents DOUBLE PRECISION NULL,
    fillable_qty INTEGER NULL,
    liquidity_sufficient BOOLEAN NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_created_at
ON signals (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_signals_type_market
ON signals (signal_type, market_ticker, created_at DESC);

CREATE TABLE IF NOT EXISTS paper_trade_orders (
    id BIGSERIAL PRIMARY KEY,
    market_ticker TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    side TEXT NOT NULL,
    count INTEGER NOT NULL,
    limit_price_cents INTEGER NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NULL,
    external_order_id TEXT NULL,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_trade_orders_created_at
ON paper_trade_orders (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_paper_trade_orders_market_direction
ON paper_trade_orders (market_ticker, direction, created_at DESC);

CREATE TABLE IF NOT EXISTS paper_trade_order_events (
    id BIGSERIAL PRIMARY KEY,
    order_id BIGINT NULL REFERENCES paper_trade_orders(id) ON DELETE CASCADE,
    market_ticker TEXT NOT NULL,
    external_order_id TEXT NULL,
    status TEXT NOT NULL,
    queue_position INTEGER NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_trade_order_events_order_id
ON paper_trade_order_events (order_id, event_ts DESC);

CREATE TABLE IF NOT EXISTS market_resolutions (
    ticker TEXT PRIMARY KEY,
    series_ticker TEXT NULL,
    event_ticker TEXT NULL,
    market_type TEXT NOT NULL,
    resolved_at TIMESTAMPTZ NULL,
    result TEXT NULL,
    actual_value DOUBLE PRECISION NULL,
    resolution_source TEXT NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_market_resolutions_type_resolved_at
ON market_resolutions (market_type, resolved_at DESC);

CREATE TABLE IF NOT EXISTS prediction_accuracy (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT NULL REFERENCES signals(id) ON DELETE SET NULL,
    ticker TEXT NOT NULL,
    signal_time TIMESTAMPTZ NOT NULL,
    model_prob DOUBLE PRECISION NULL,
    market_prob DOUBLE PRECISION NULL,
    edge_bps DOUBLE PRECISION NULL,
    actual_outcome BOOLEAN NULL,
    pnl_per_contract DOUBLE PRECISION NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prediction_accuracy_ticker
ON prediction_accuracy (ticker, signal_time DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_accuracy_signal_id_unique
ON prediction_accuracy (signal_id)
WHERE signal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS bracket_arb_opportunities (
    id BIGSERIAL PRIMARY KEY,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_ticker TEXT NOT NULL,
    arb_type TEXT NOT NULL,
    n_brackets INTEGER NOT NULL,
    cost_cents INTEGER NOT NULL,
    payout_cents INTEGER NOT NULL,
    profit_cents INTEGER NOT NULL,
    profit_after_fees_cents INTEGER NOT NULL,
    max_sets INTEGER NOT NULL,
    total_profit_cents INTEGER NOT NULL,
    legs JSONB NOT NULL DEFAULT '[]'::jsonb,
    executed BOOLEAN NOT NULL DEFAULT FALSE,
    execution_result JSONB NULL
);

CREATE INDEX IF NOT EXISTS idx_bracket_arb_detected_at
ON bracket_arb_opportunities (detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_bracket_arb_event_ticker
ON bracket_arb_opportunities (event_ticker, detected_at DESC);

CREATE TABLE IF NOT EXISTS alert_events (
    id BIGSERIAL PRIMARY KEY,
    channel TEXT NOT NULL,
    event_type TEXT NOT NULL,
    market_ticker TEXT NULL,
    message TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alert_events_created_at
ON alert_events (created_at DESC);

ALTER TABLE signals ADD COLUMN IF NOT EXISTS data_source TEXT NOT NULL DEFAULT 'rest';
ALTER TABLE signals ADD COLUMN IF NOT EXISTS vwap_cents DOUBLE PRECISION NULL;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS fillable_qty INTEGER NULL;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS liquidity_sufficient BOOLEAN NULL;
