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
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_signals_created_at
ON signals (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_signals_type_market
ON signals (signal_type, market_ticker, created_at DESC);
