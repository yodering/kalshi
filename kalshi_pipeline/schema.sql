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

