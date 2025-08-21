-- ============================================================================
-- trading_all_in_one.sql  (PG 15/16/17 + TimescaleDB)
-- Unificado: Macro + Técnico + Analytics (idempotente, long-only, UTC)
-- ============================================================================


-- Este script crea las tablas, tipos, vistas y funciones necesarias para el sistema de trading.
-- 1) Schemas
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS analytics;

-- ============================================================================
-- TIPOS ENUM (técnico)
-- ============================================================================
DO $$
BEGIN
  DROP TYPE IF EXISTS core.tf_enum CASCADE;
  DROP TYPE IF EXISTS core.style_enum CASCADE;
  DROP TYPE IF EXISTS core.group_enum CASCADE;
  DROP TYPE IF EXISTS core.box_state_enum CASCADE;
  DROP TYPE IF EXISTS core.tl_type_enum CASCADE;
  DROP TYPE IF EXISTS core.tl_state_enum CASCADE;
  DROP TYPE IF EXISTS core.event_type_enum CASCADE;

  CREATE TYPE core.tf_enum AS ENUM ('M1','M5','M15','M30','H1','H4','D1','W1','MN1');
  CREATE TYPE core.style_enum AS ENUM ('intraday','swing');
  CREATE TYPE core.group_enum AS ENUM ('core','extend','greed');
  CREATE TYPE core.box_state_enum AS ENUM ('active','broken','retest_hold');
  CREATE TYPE core.tl_type_enum AS ENUM ('bull','bear');
  CREATE TYPE core.tl_state_enum AS ENUM ('active','broken');
  CREATE TYPE core.event_type_enum AS ENUM (
    'breakout_res_confirmed','retest_res_hold',
    'breakout_sup_confirmed','retest_sup_hold',
    'tl_bull_break','tl_bear_break','fract_up','fract_dn'
  );
END$$;

-- ============================================================================
-- MACRO
-- ============================================================================

-- 2) Serie temporal de features macro (normalizada a ts_utc)
CREATE TABLE IF NOT EXISTS core.macro_ticks (
  ts_utc            TIMESTAMPTZ NOT NULL,
  feature           TEXT NOT NULL,  -- VIX, DXY, UST10Y, etc.
  value             NUMERIC NOT NULL,
  aux_values        JSONB NOT NULL DEFAULT '{}'::jsonb,
  source_id         TEXT NOT NULL,  -- yfinance, fred, derived, mt5, etc.
  method            TEXT NOT NULL CHECK (method IN ('official_api','unofficial_api','scrape','public_json','calc')),
  delay_sec         INTEGER NOT NULL DEFAULT 0,
  feature_age_sec   INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL CHECK (status IN ('healthy','degraded','manual')),
  valid_until_ts    TIMESTAMPTZ,
  ingest_latency_ms INTEGER NOT NULL DEFAULT 0,
  is_historic       BOOLEAN DEFAULT FALSE,
  source_type       TEXT,
  ingest_source     TEXT,
  ingested_at       TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (ts_utc, feature)
);
SELECT create_hypertable('core.macro_ticks','ts_utc', if_not_exists => TRUE);

ALTER TABLE core.macro_ticks SET (
  timescaledb.compress,
  timescaledb.compress_orderby = 'ts_utc DESC',
  timescaledb.compress_segmentby = 'feature'
);
CREATE INDEX IF NOT EXISTS ix_macro_ticks_feature_ts ON core.macro_ticks (feature, ts_utc DESC);
CREATE INDEX IF NOT EXISTS ix_macro_ticks_status      ON core.macro_ticks (status);
CREATE INDEX IF NOT EXISTS idx_macro_historic         ON core.macro_ticks (is_historic) WHERE is_historic = false;
SELECT add_compression_policy('core.macro_ticks', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy  ('core.macro_ticks', INTERVAL '1 year', if_not_exists => TRUE);

-- Upsert macro (idempotente)
CREATE OR REPLACE FUNCTION core.upsert_macro_tick(
  p_ts_utc            TIMESTAMPTZ,
  p_feature           TEXT,
  p_value             NUMERIC,
  p_aux_values        JSONB,
  p_source_id         TEXT,
  p_method            TEXT,
  p_status            TEXT,
  p_valid_until_ts    TIMESTAMPTZ,
  p_ingest_latency_ms INTEGER,
  p_is_historic       BOOLEAN DEFAULT FALSE,
  p_source_type       TEXT DEFAULT NULL,
  p_ingest_source     TEXT DEFAULT NULL
) RETURNS VOID AS $$
BEGIN
  INSERT INTO core.macro_ticks (
    ts_utc, feature, value, aux_values, source_id, method, status,
    valid_until_ts, ingest_latency_ms, is_historic, source_type, ingest_source
  ) VALUES (
    p_ts_utc, p_feature, p_value, COALESCE(p_aux_values,'{}'::jsonb),
    p_source_id, p_method, p_status, p_valid_until_ts,
    COALESCE(p_ingest_latency_ms,0), p_is_historic, p_source_type, p_ingest_source
  )
  ON CONFLICT (ts_utc, feature) DO UPDATE SET
    value             = EXCLUDED.value,
    aux_values        = EXCLUDED.aux_values,
    source_id         = EXCLUDED.source_id,
    method            = EXCLUDED.method,
    status            = EXCLUDED.status,
    valid_until_ts    = EXCLUDED.valid_until_ts,
    ingest_latency_ms = EXCLUDED.ingest_latency_ms,
    is_historic       = EXCLUDED.is_historic,
    source_type       = EXCLUDED.source_type,
    ingest_source     = EXCLUDED.ingest_source,
    ingested_at       = NOW();
END; $$ LANGUAGE plpgsql;

-- Continuous aggregates (5m / 15m / 1h) sobre macro_ticks
DO $$
BEGIN
  IF to_regclass('core.cagg_macro_5m')  IS NOT NULL THEN EXECUTE 'DROP MATERIALIZED VIEW core.cagg_macro_5m';  END IF;
  IF to_regclass('core.cagg_macro_15m') IS NOT NULL THEN EXECUTE 'DROP MATERIALIZED VIEW core.cagg_macro_15m'; END IF;
  IF to_regclass('core.cagg_macro_1h')  IS NOT NULL THEN EXECUTE 'DROP MATERIALIZED VIEW core.cagg_macro_1h';  END IF;
END$$;

CREATE MATERIALIZED VIEW core.cagg_macro_5m
WITH (timescaledb.continuous) AS
SELECT
  time_bucket(INTERVAL '5 minutes', ts_utc) AS bucket,
  feature,
  (ARRAY_AGG(value ORDER BY ts_utc DESC))[1] AS value_last,
  MAX(valid_until_ts) AS valid_until_ts,
  MAX(status) AS status
FROM core.macro_ticks
GROUP BY bucket, feature
WITH NO DATA;

CREATE MATERIALIZED VIEW core.cagg_macro_15m
WITH (timescaledb.continuous) AS
SELECT
  time_bucket(INTERVAL '15 minutes', ts_utc) AS bucket,
  feature,
  (ARRAY_AGG(value ORDER BY ts_utc DESC))[1] AS value_last,
  MAX(valid_until_ts) AS valid_until_ts,
  MAX(status) AS status
FROM core.macro_ticks
GROUP BY bucket, feature
WITH NO DATA;

CREATE MATERIALIZED VIEW core.cagg_macro_1h
WITH (timescaledb.continuous) AS
SELECT
  time_bucket(INTERVAL '1 hour', ts_utc) AS bucket,
  feature,
  (ARRAY_AGG(value ORDER BY ts_utc DESC))[1] AS value_last,
  MAX(valid_until_ts) AS valid_until_ts,
  MAX(status) AS status
FROM core.macro_ticks
GROUP BY bucket, feature
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
  'core.cagg_macro_5m',  start_offset => INTERVAL '7 days',   end_offset => INTERVAL '1 minute',  schedule_interval => INTERVAL '1 minute');
SELECT add_continuous_aggregate_policy(
  'core.cagg_macro_15m', start_offset => INTERVAL '30 days',  end_offset => INTERVAL '5 minutes', schedule_interval => INTERVAL '5 minutes');
SELECT add_continuous_aggregate_policy(
  'core.cagg_macro_1h',  start_offset => INTERVAL '180 days', end_offset => INTERVAL '15 minutes',schedule_interval => INTERVAL '15 minutes');

CREATE INDEX IF NOT EXISTS ix_cagg_macro_5m_feat_bucket
  ON core.cagg_macro_5m  (feature, bucket DESC)  INCLUDE (value_last, status, valid_until_ts);
CREATE INDEX IF NOT EXISTS ix_cagg_macro_15m_feat_bucket
  ON core.cagg_macro_15m (feature, bucket DESC)  INCLUDE (value_last, status, valid_until_ts);
CREATE INDEX IF NOT EXISTS ix_cagg_macro_1h_feat_bucket
  ON core.cagg_macro_1h  (feature, bucket DESC)  INCLUDE (value_last, status, valid_until_ts);

-- Vista: último valor por feature
CREATE OR REPLACE VIEW core.v_macro_latest AS
SELECT DISTINCT ON (feature)
  feature, value, ts_utc, aux_values, status,
  (valid_until_ts < now()) AS expired,
  FALSE AS is_overridden
FROM core.macro_ticks
ORDER BY feature, ts_utc DESC;

-- 3) Estado macro + auditoría + calendario + overrides
CREATE TABLE IF NOT EXISTS core.macro_state (
  ts               TIMESTAMPTZ NOT NULL,
  tier             TEXT NOT NULL,               -- 'fast' | 'slow' | 'fused'
  long_permission  BOOLEAN NOT NULL,
  risk_multiplier  NUMERIC NOT NULL,            -- 1.0, 0.5, 0.25
  allowed_groups   TEXT[] NOT NULL,             -- ['CORE','EXTENSION','GREED']
  prioritize       TEXT[] NOT NULL,
  avoid            TEXT[] NOT NULL,
  reason           TEXT,
  meta             JSONB NOT NULL DEFAULT '{}'::jsonb,
  PRIMARY KEY (ts, tier)
);
SELECT create_hypertable('core.macro_state','ts', if_not_exists => TRUE);
ALTER TABLE core.macro_state SET (timescaledb.compress, timescaledb.compress_orderby='ts DESC', timescaledb.compress_segmentby='tier');
CREATE INDEX IF NOT EXISTS idx_macro_state_tier_ts ON core.macro_state (tier, ts DESC);
CREATE INDEX IF NOT EXISTS idx_macro_state_ts      ON core.macro_state (ts DESC);
SELECT add_compression_policy('core.macro_state', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy  ('core.macro_state', INTERVAL '90 days', if_not_exists => TRUE);

CREATE OR REPLACE VIEW core.macro_state_latest AS
SELECT DISTINCT ON (tier) tier, ts, long_permission, risk_multiplier, allowed_groups, prioritize, avoid, reason, meta
FROM core.macro_state ORDER BY tier, ts DESC;

CREATE OR REPLACE VIEW core.macro_state_fused_latest AS
SELECT * FROM core.macro_state WHERE tier='fused' ORDER BY ts DESC LIMIT 1;

CREATE TABLE IF NOT EXISTS core.fund_gate_audit (
  id              BIGSERIAL,
  ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
  allowed         BOOLEAN NOT NULL,
  reason          TEXT NOT NULL,
  feature_missing TEXT[] NOT NULL DEFAULT '{}',
  snapshot        JSONB NOT NULL,
  signal          JSONB NOT NULL,
  PRIMARY KEY (ts, id)  -- 👈 incluye la columna de partición
);

SELECT create_hypertable('core.fund_gate_audit','ts', if_not_exists => TRUE);
ALTER TABLE core.fund_gate_audit SET (timescaledb.compress, timescaledb.compress_orderby='ts DESC');
CREATE INDEX IF NOT EXISTS fund_gate_audit_ts_idx      ON core.fund_gate_audit (ts DESC);
CREATE INDEX IF NOT EXISTS fund_gate_audit_allowed_idx ON core.fund_gate_audit (allowed);
CREATE INDEX IF NOT EXISTS fund_gate_audit_reason_idx  ON core.fund_gate_audit (reason);
SELECT add_compression_policy('core.fund_gate_audit', INTERVAL '7 days',  if_not_exists => TRUE);
SELECT add_retention_policy  ('core.fund_gate_audit', INTERVAL '180 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS core.calendar_events (
  event_id        TEXT PRIMARY KEY,             -- hash(date+title+currency)
  event_time_utc  TIMESTAMPTZ NOT NULL,
  currency        TEXT NOT NULL,
  impact          TEXT NOT NULL CHECK (impact IN ('low','medium','high')),
  title           TEXT NOT NULL,
  forecast        TEXT,
  previous        TEXT,
  actual          TEXT,
  status          TEXT NOT NULL CHECK (status IN ('upcoming','released','revised')),
  source_id       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_calendar_time ON core.calendar_events (event_time_utc);

CREATE TABLE IF NOT EXISTS core.manual_overrides (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ts_created     TIMESTAMPTZ NOT NULL DEFAULT now(),
  feature        TEXT NOT NULL,
  value          NUMERIC NOT NULL,
  ttl_sec        INTEGER NOT NULL DEFAULT 900,
  valid_until_ts TIMESTAMPTZ GENERATED ALWAYS AS (ts_created + make_interval(secs => ttl_sec)) STORED,
  operator       TEXT,
  reason         TEXT,
  notes          TEXT
);

-- ============================================================================
-- MERCADO / TÉCNICO
-- ============================================================================

-- 4) Símbolos (catálogo usado por tablas técnicas)
CREATE TABLE IF NOT EXISTS core.symbols (
  symbol        TEXT PRIMARY KEY,
  asset_class   TEXT NOT NULL,
  risk_group    core.group_enum NOT NULL,
  pip_value     DOUBLE PRECISION,
  tick_size     DOUBLE PRECISION,
  lot_min       DOUBLE PRECISION DEFAULT 0.01,
  lot_step      DOUBLE PRECISION DEFAULT 0.01,
  min_distance_pt DOUBLE PRECISION DEFAULT 10,
  trading_hours JSONB DEFAULT '[]'::jsonb,
  is_enabled    BOOLEAN DEFAULT TRUE,
  meta          JSONB DEFAULT '{}'::jsonb
);

-- 5) Velas OHLC principales
CREATE TABLE IF NOT EXISTS core.market_candles (
  symbol       TEXT NOT NULL,
  tf           core.tf_enum NOT NULL,
  ts_utc       TIMESTAMPTZ NOT NULL,
  open         DOUBLE PRECISION NOT NULL,
  high         DOUBLE PRECISION NOT NULL,
  low          DOUBLE PRECISION NOT NULL,
  close        DOUBLE PRECISION NOT NULL,
  tick_volume  BIGINT,
  spread       DOUBLE PRECISION,
  ingest_ts    TIMESTAMPTZ DEFAULT NOW(),
  source_id    TEXT DEFAULT 'mt5',
  quality      SMALLINT DEFAULT 100,
  PRIMARY KEY (symbol, tf, ts_utc)
);
SELECT create_hypertable('core.market_candles','ts_utc', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
ALTER TABLE core.market_candles SET (timescaledb.compress, timescaledb.compress_orderby='ts_utc DESC', timescaledb.compress_segmentby='symbol, tf');
CREATE INDEX IF NOT EXISTS idx_mc_symbol_tf_ts ON core.market_candles (symbol, tf, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_mc_ts_symbol    ON core.market_candles (ts_utc DESC, symbol);
CREATE INDEX IF NOT EXISTS idx_mc_symbol_quality ON core.market_candles (symbol, quality) WHERE quality < 90;
SELECT add_compression_policy('core.market_candles', INTERVAL '3 days', if_not_exists => TRUE);
SELECT add_retention_policy  ('core.market_candles', INTERVAL '2 years', if_not_exists => TRUE);

-- 6) Features técnicos
-- ========= FEATURES: crear / arreglar =========

-- 1) Crear con PK correcta si no existe
CREATE TABLE IF NOT EXISTS core.features (
  symbol       TEXT NOT NULL,
  tf           core.tf_enum NOT NULL,
  ts_utc       TIMESTAMPTZ NOT NULL,
  ema20        DOUBLE PRECISION,
  ema50        DOUBLE PRECISION,
  ema200       DOUBLE PRECISION,
  vwap         DOUBLE PRECISION,
  vwap_sigma1  DOUBLE PRECISION,
  vwap_sigma2  DOUBLE PRECISION,
  atr          DOUBLE PRECISION,
  atr_norm     DOUBLE PRECISION,
  rsi          DOUBLE PRECISION,
  roc          DOUBLE PRECISION,
  macd         DOUBLE PRECISION,
  macd_signal  DOUBLE PRECISION,
  macd_hist    DOUBLE PRECISION,
  boll_upper   DOUBLE PRECISION,
  boll_middle  DOUBLE PRECISION,
  boll_lower   DOUBLE PRECISION,
  stoch_k      DOUBLE PRECISION,
  stoch_d      DOUBLE PRECISION,
  extra        JSONB DEFAULT '{}'::jsonb,
  calc_time_ms INTEGER,
  PRIMARY KEY (symbol, tf, ts_utc)   -- ✅ incluye columna de partición
);

-- 2) Si venías con 'ts' o una PK incorrecta, lo corregimos
DO $$
DECLARE
  has_ts_utc  bool;
  has_ts      bool;
  pk_ok       bool;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='core' AND table_name='features' AND column_name='ts_utc'
  ) INTO has_ts_utc;

  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='core' AND table_name='features' AND column_name='ts'
  ) INTO has_ts;

  IF NOT has_ts_utc THEN
    IF has_ts THEN
      EXECUTE 'ALTER TABLE core.features RENAME COLUMN ts TO ts_utc';
    ELSE
      EXECUTE 'ALTER TABLE core.features ADD COLUMN ts_utc timestamptz NOT NULL DEFAULT now()';
    END IF;
  END IF;

  -- PK debe incluir ts_utc
  SELECT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=r.relnamespace
    WHERE c.contype='p' AND n.nspname='core' AND r.relname='features'
      AND EXISTS (
        SELECT 1 FROM unnest(c.conkey) k
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k
        WHERE a.attname='ts_utc'
      )
  ) INTO pk_ok;

  IF NOT pk_ok THEN
    EXECUTE 'ALTER TABLE core.features DROP CONSTRAINT IF EXISTS features_pkey';
    EXECUTE 'ALTER TABLE core.features ADD PRIMARY KEY (symbol, tf, ts_utc)';
  END IF;
END$$;

-- 3) Hypertable + compresión + policies
SELECT create_hypertable('core.features','ts_utc',
  chunk_time_interval => INTERVAL '7 days',
  if_not_exists => TRUE);

ALTER TABLE core.features SET (
  timescaledb.compress,
  timescaledb.compress_orderby='ts_utc DESC',
  timescaledb.compress_segmentby='symbol, tf'
);

-- 4) Índices útiles
CREATE INDEX IF NOT EXISTS idx_features_symbol_tf_ts
  ON core.features (symbol, tf, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_features_technical
  ON core.features (symbol, tf, ts_utc DESC)
  INCLUDE (ema20, ema50, rsi, atr);

-- 5) Policies
SELECT add_compression_policy('core.features', INTERVAL '3 days', if_not_exists => TRUE);
SELECT add_retention_policy  ('core.features', INTERVAL '1 year', if_not_exists => TRUE);


-- 7) Eventos técnicos
-- ========= TECH_EVENTS: crear/arreglar =========

-- 1) Crear tabla si no existe (PK correcta con columna de tiempo primero)
CREATE TABLE IF NOT EXISTS core.tech_events (
  event_id     BIGSERIAL,
  symbol       TEXT NOT NULL,
  tf           core.tf_enum NOT NULL,
  ts_utc       TIMESTAMPTZ NOT NULL,
  event_type   core.event_type_enum NOT NULL,
  level        DOUBLE PRECISION,
  ref_box_id   BIGINT,
  ref_tl_id    BIGINT,
  features_snapshot JSONB NOT NULL,
  confidence   DOUBLE PRECISION DEFAULT 0,
  volume_ratio DOUBLE PRECISION DEFAULT 1,
  meta         JSONB DEFAULT '{}'::jsonb,
  PRIMARY KEY (ts_utc, event_id)   -- ✅ incluye la columna de partición
);

-- 2) Si ya existía con otra PK/columna, la corregimos
DO $$
DECLARE
  has_ts_utc bool;
  pk_has_ts  bool;
BEGIN
  -- Asegura que exista ts_utc (si venías con 'ts', lo renombramos)
  SELECT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='core' AND table_name='tech_events' AND column_name='ts_utc'
  ) INTO has_ts_utc;

  IF NOT has_ts_utc THEN
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='core' AND table_name='tech_events' AND column_name='ts'
    ) THEN
      EXECUTE 'ALTER TABLE core.tech_events RENAME COLUMN ts TO ts_utc';
    ELSE
      EXECUTE 'ALTER TABLE core.tech_events ADD COLUMN ts_utc timestamptz NOT NULL DEFAULT now()';
    END IF;
  END IF;

  -- PK debe incluir ts_utc
  SELECT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=r.relnamespace
    WHERE c.contype='p' AND n.nspname='core' AND r.relname='tech_events'
      AND EXISTS (
        SELECT 1 FROM unnest(c.conkey) k
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k
        WHERE a.attname='ts_utc'
      )
  ) INTO pk_has_ts;

  IF NOT pk_has_ts THEN
    EXECUTE 'ALTER TABLE core.tech_events DROP CONSTRAINT IF EXISTS tech_events_pkey';
    EXECUTE 'ALTER TABLE core.tech_events ADD PRIMARY KEY (ts_utc, event_id)';
  END IF;
END$$;

-- 3) Convertir a hypertable (idempotente)
SELECT create_hypertable('core.tech_events','ts_utc',
  chunk_time_interval => INTERVAL '30 days',
  if_not_exists => TRUE);

-- 4) Compresión + índices + políticas (idempotente)
ALTER TABLE core.tech_events SET (
  timescaledb.compress,
  timescaledb.compress_orderby='ts_utc DESC',
  timescaledb.compress_segmentby='symbol, tf'
);

CREATE INDEX IF NOT EXISTS idx_events_symbol_tf_ts   ON core.tech_events (symbol, tf, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_ts        ON core.tech_events (event_type, ts_utc DESC);
CREATE INDEX IF NOT EXISTS idx_events_confidence     ON core.tech_events (confidence) WHERE confidence > 0.7;
CREATE INDEX IF NOT EXISTS idx_events_sym_tf_type_ts ON core.tech_events (symbol, tf, event_type, ts_utc DESC);

SELECT add_compression_policy('core.tech_events', INTERVAL '14 days', if_not_exists => TRUE);
SELECT add_retention_policy  ('core.tech_events', INTERVAL '6 months', if_not_exists => TRUE);


-- 8) Boxes / Trendlines

CREATE TABLE IF NOT EXISTS core.pivots_boxes (
  box_id       BIGSERIAL PRIMARY KEY,
  symbol       TEXT NOT NULL REFERENCES core.symbols(symbol),
  tf           core.tf_enum NOT NULL,
  ts_created   TIMESTAMPTZ NOT NULL,
  top          DOUBLE PRECISION NOT NULL,
  bottom       DOUBLE PRECISION NOT NULL,
  state        core.box_state_enum NOT NULL,
  atr_k        DOUBLE PRECISION,
  vol_z        DOUBLE PRECISION,
  ts_updated   TIMESTAMPTZ DEFAULT NOW(),
  touches      INTEGER DEFAULT 0,
  age_bars     INTEGER DEFAULT 0,
  meta         JSONB DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS core.trendlines (
  tl_id        BIGSERIAL PRIMARY KEY,
  symbol       TEXT NOT NULL REFERENCES core.symbols(symbol),
  tf           core.tf_enum NOT NULL,
  ts_start     TIMESTAMPTZ NOT NULL,
  ts_last      TIMESTAMPTZ NOT NULL,
  x1           BIGINT,
  y1           DOUBLE PRECISION,
  x2           BIGINT,
  y2           DOUBLE PRECISION,
  slope        DOUBLE PRECISION,
  type         core.tl_type_enum NOT NULL,
  state        core.tl_state_enum NOT NULL,
  touches      INTEGER DEFAULT 0,
  break_count  INTEGER DEFAULT 0,
  meta         JSONB DEFAULT '{}'::jsonb
);

-- 9) Señales / Órdenes / Fills / Webhooks / Logs

CREATE TABLE IF NOT EXISTS core.signals (
  signal_id    BIGSERIAL PRIMARY KEY,
  setup_id     TEXT NOT NULL,
  style        core.style_enum NOT NULL,
  symbol       TEXT NOT NULL REFERENCES core.symbols(symbol),
  tf           core.tf_enum NOT NULL,
  ts_utc       TIMESTAMPTZ NOT NULL,
  entry_plan   TEXT NOT NULL,
  sl_plan      TEXT NOT NULL,
  tp1_rr       DOUBLE PRECISION,
  confidence   TEXT,
  scoring_breakdown JSONB NOT NULL,
  rejected_reason TEXT,
  priority     INTEGER DEFAULT 50,
  expires_at   TIMESTAMPTZ,
  meta         JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_signals_expires        ON core.signals (expires_at);
CREATE INDEX IF NOT EXISTS idx_signals_symbol_tf_ts   ON core.signals (symbol, tf, ts_utc DESC);

CREATE TABLE IF NOT EXISTS core.orders (
  order_id     BIGSERIAL PRIMARY KEY,
  signal_id    BIGINT NOT NULL REFERENCES core.signals(signal_id),
  side         TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
  intent       TEXT NOT NULL CHECK (intent IN ('open','close','tp','sl')) DEFAULT 'open',
  qty          DOUBLE PRECISION NOT NULL,
  type         TEXT NOT NULL CHECK (type IN ('market', 'limit', 'stop')),
  price        DOUBLE PRECISION,
  sl_price     DOUBLE PRECISION,
  tp_price     DOUBLE PRECISION,
  ts_sent      TIMESTAMPTZ NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'filled', 'canceled', 'rejected')),
  filled_qty   DOUBLE PRECISION DEFAULT 0,
  avg_price    DOUBLE PRECISION,
  router_meta  JSONB DEFAULT '{}'::jsonb,
  CONSTRAINT chk_long_only CHECK (NOT (intent = 'open' AND side = 'sell'))
);
CREATE INDEX IF NOT EXISTS idx_orders_status_time ON core.orders (status, ts_sent DESC);

CREATE TABLE IF NOT EXISTS core.fills (
  fill_id     BIGSERIAL PRIMARY KEY,
  order_id    BIGINT NOT NULL REFERENCES core.orders(order_id),
  ts_utc      TIMESTAMPTZ NOT NULL,
  qty         DOUBLE PRECISION NOT NULL,
  price       DOUBLE PRECISION NOT NULL,
  commission  DOUBLE PRECISION DEFAULT 0,
  slippage_pt DOUBLE PRECISION,
  venue       TEXT,
  meta        JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_fills_order_ts ON core.fills(order_id, ts_utc DESC);


CREATE TABLE IF NOT EXISTS core.router_logs (
  log_id      BIGSERIAL PRIMARY KEY,
  ts_utc      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  level       TEXT CHECK (level IN ('INFO','WARN','ERROR')) NOT NULL,
  component   TEXT NOT NULL,
  ref_id      TEXT,
  message     TEXT NOT NULL,
  meta        JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_router_logs_ts ON core.router_logs (ts_utc DESC, level);

-- ============ POSITIONS: crear/arreglar ============

-- 1) Crear tabla si no existe (PK con columna de tiempo primero: mejor para TSDB)
CREATE TABLE IF NOT EXISTS core.positions (
  position_id   BIGSERIAL,
  symbol        TEXT NOT NULL,
  style         core.style_enum NOT NULL,
  ts_open       TIMESTAMPTZ NOT NULL,
  ts_close      TIMESTAMPTZ,
  size          DOUBLE PRECISION NOT NULL,
  entry_price   DOUBLE PRECISION NOT NULL,
  exit_price    DOUBLE PRECISION,
  pnl_nominal   DOUBLE PRECISION,
  pnl_R         DOUBLE PRECISION,
  mfe           DOUBLE PRECISION,
  mae           DOUBLE PRECISION,
  pyramid_count INT DEFAULT 0,
  reentry_flag  BOOLEAN DEFAULT FALSE,
  setup_id      TEXT NOT NULL,
  signal_id     BIGINT,
  exit_reason   TEXT,
  meta          JSONB DEFAULT '{}'::jsonb,
  PRIMARY KEY (ts_open, position_id)   -- 👈 time + id (orden recomendado)
);

-- 2) Si ya existía con otra PK (p.ej. (position_id, ts_open)), la corregimos
DO $$
DECLARE
  pk_has_ts BOOL;
BEGIN
  SELECT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid=c.conrelid
    JOIN pg_namespace n ON n.oid=r.relnamespace
    WHERE c.contype='p' AND n.nspname='core' AND r.relname='positions'
      AND EXISTS (
        SELECT 1
        FROM unnest(c.conkey) k
        JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k
        WHERE a.attname='ts_open'
      )
  ) INTO pk_has_ts;

  IF NOT pk_has_ts THEN
    EXECUTE 'ALTER TABLE core.positions DROP CONSTRAINT IF EXISTS positions_pkey';
    EXECUTE 'ALTER TABLE core.positions ADD PRIMARY KEY (ts_open, position_id)';
  END IF;
END$$;

-- 3) Convertir a hypertable (idempotente)
SELECT create_hypertable('core.positions','ts_open',
  chunk_time_interval => INTERVAL '30 days',
  if_not_exists => TRUE);

-- 4) Índices útiles
CREATE INDEX IF NOT EXISTS idx_positions_symbol_style ON core.positions (symbol, style);
CREATE INDEX IF NOT EXISTS idx_positions_open         ON core.positions (ts_open) WHERE ts_close IS NULL;
CREATE INDEX IF NOT EXISTS idx_positions_pnl          ON core.positions (pnl_R) WHERE pnl_R IS NOT NULL;

-- 5) FK a signals si existe la tabla (idempotente)
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema='core' AND table_name='signals') THEN
    ALTER TABLE core.positions
      DROP CONSTRAINT IF EXISTS fk_positions_signal;
    ALTER TABLE core.positions
      ADD CONSTRAINT fk_positions_signal
      FOREIGN KEY (signal_id) REFERENCES core.signals(signal_id);
  END IF;
END$$;


-- ============================================================================
-- VISTAS ÚTILES
-- ============================================================================

-- Velas del día actual (UTC)
CREATE OR REPLACE VIEW core.v_candles_today AS
SELECT symbol, tf, ts_utc, open, high, low, close, tick_volume AS volume, spread
FROM core.market_candles
WHERE ts_utc >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
  AND ts_utc <  date_trunc('day', NOW() AT TIME ZONE 'UTC') + INTERVAL '1 day'
ORDER BY ts_utc DESC;

-- Últimos valores por símbolo y timeframe
CREATE OR REPLACE VIEW core.v_latest_candles AS
WITH ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY symbol, tf ORDER BY ts_utc DESC) as rn
  FROM core.market_candles
)
SELECT symbol, tf, ts_utc, open, high, low, close, tick_volume AS volume, spread
FROM ranked
WHERE rn = 1;

-- Posiciones abiertas (join por signal_id)
CREATE OR REPLACE VIEW core.v_open_positions AS
SELECT
  p.position_id, p.symbol, p.style, p.ts_open, p.ts_close,
  p.size, p.entry_price, p.exit_price, p.pnl_nominal, p.pnl_R,
  p.mfe, p.mae, p.pyramid_count, p.reentry_flag, p.setup_id,
  p.signal_id, p.exit_reason, p.meta,
  s.symbol AS signal_symbol,
  s.setup_id AS signal_setup_id,
  s.style  AS signal_style,
  s.tf, s.ts_utc, s.priority
FROM core.positions p
LEFT JOIN core.signals s
  ON p.signal_id = s.signal_id
WHERE p.ts_close IS NULL;


-- Resumen diario de PnL (vista directa)
CREATE OR REPLACE VIEW core.v_daily_pnl AS
SELECT
  DATE(ts_close) as trade_date,
  symbol,
  style,
  COUNT(*) as trades,
  SUM(pnl_nominal) as total_pnl,
  AVG(pnl_R) as avg_r_multiple,
  SUM(CASE WHEN pnl_nominal > 0 THEN 1 ELSE 0 END) as winners,
  SUM(CASE WHEN pnl_nominal <= 0 THEN 1 ELSE 0 END) as losers
FROM core.positions
WHERE ts_close IS NOT NULL
GROUP BY trade_date, symbol, style
ORDER BY trade_date DESC;

-- ============================================================================
-- ANALYTICS
-- ============================================================================

-- Equity curve (placeholder)
CREATE TABLE IF NOT EXISTS analytics.equity_curve (
  ts          TIMESTAMPTZ NOT NULL,
  equity      NUMERIC NOT NULL,
  PRIMARY KEY (ts)
);
SELECT create_hypertable('analytics.equity_curve','ts', if_not_exists => TRUE);

-- Continuous aggregate para PnL diario
-- 1) Tabla de cierres (time key = ts_close)
CREATE TABLE IF NOT EXISTS core.positions_closes (
  ts_close     TIMESTAMPTZ NOT NULL,
  position_id  BIGINT      NOT NULL,
  symbol       TEXT        NOT NULL,
  style        core.style_enum NOT NULL,
  pnl_nominal  DOUBLE PRECISION,
  pnl_R        DOUBLE PRECISION,
  meta         JSONB DEFAULT '{}'::jsonb,
  PRIMARY KEY (ts_close, position_id)  -- incluir la columna temporal en la PK
);

-- Hypertable + compresión
SELECT create_hypertable('core.positions_closes','ts_close',
  chunk_time_interval => INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE core.positions_closes SET (
  timescaledb.compress,
  timescaledb.compress_orderby = 'ts_close DESC',
  timescaledb.compress_segmentby = 'symbol, style'
);

CREATE INDEX IF NOT EXISTS idx_poscl_symbol_style_ts
  ON core.positions_closes (symbol, style, ts_close DESC);

-- 2) Backfill histórico desde positions
INSERT INTO core.positions_closes (ts_close, position_id, symbol, style, pnl_nominal, pnl_R, meta)
SELECT ts_close, position_id, symbol, style, pnl_nominal, pnl_R, meta
FROM core.positions
WHERE ts_close IS NOT NULL
ON CONFLICT DO NOTHING;

-- 3) Trigger para mantener sincronizado cuando se cierra una posición
CREATE OR REPLACE FUNCTION core.sync_positions_closes() RETURNS trigger AS $$
BEGIN
  -- Inserta/actualiza cuando ts_close pasa de NULL -> valor (o cambia)
  IF NEW.ts_close IS NOT NULL AND (TG_OP='INSERT' OR OLD.ts_close IS DISTINCT FROM NEW.ts_close) THEN
    INSERT INTO core.positions_closes (ts_close, position_id, symbol, style, pnl_nominal, pnl_R, meta)
    VALUES (NEW.ts_close, NEW.position_id, NEW.symbol, NEW.style, NEW.pnl_nominal, NEW.pnl_R, COALESCE(NEW.meta,'{}'::jsonb))
    ON CONFLICT (ts_close, position_id) DO UPDATE
      SET symbol = EXCLUDED.symbol,
          style  = EXCLUDED.style,
          pnl_nominal = EXCLUDED.pnl_nominal,
          pnl_R = EXCLUDED.pnl_R,
          meta  = EXCLUDED.meta;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_sync_positions_closes ON core.positions;
CREATE TRIGGER trg_sync_positions_closes
AFTER INSERT OR UPDATE OF ts_close, pnl_nominal, pnl_R, meta ON core.positions
FOR EACH ROW EXECUTE FUNCTION core.sync_positions_closes();

-- 4) CAGG diario por fecha de cierre
CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.ca_daily_pnl
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', ts_close) AS day_utc,
       symbol, style,
       COUNT(*)::bigint               AS trades,
       SUM(pnl_nominal)               AS total_pnl,
       AVG(pnl_R)                     AS avg_r_multiple,
       SUM((pnl_nominal > 0)::int)    AS winners,
       SUM((pnl_nominal <= 0)::int)   AS losers
FROM core.positions_closes
GROUP BY day_utc, symbol, style
WITH NO DATA;

-- Política de refresco
SELECT add_continuous_aggregate_policy(
  'analytics.ca_daily_pnl',
  start_offset => INTERVAL '90 days',
  end_offset   => INTERVAL '1 hour',
  schedule_interval => INTERVAL '5 minutes');


-- ============================================================================
-- FUNCIONES DE UTILIDAD (técnico)
-- ============================================================================
CREATE OR REPLACE FUNCTION core.upsert_candle(
  p_symbol TEXT,
  p_tf core.tf_enum,
  p_ts_utc TIMESTAMPTZ,
  p_open DOUBLE PRECISION,
  p_high DOUBLE PRECISION,
  p_low DOUBLE PRECISION,
  p_close DOUBLE PRECISION,
  p_volume BIGINT DEFAULT NULL,
  p_spread DOUBLE PRECISION DEFAULT NULL,
  p_source TEXT DEFAULT 'mt5'
) RETURNS VOID AS $$
BEGIN
  INSERT INTO core.market_candles (symbol, tf, ts_utc, open, high, low, close, tick_volume, spread, source_id)
  VALUES (p_symbol, p_tf, p_ts_utc, p_open, p_high, p_low, p_close, p_volume, p_spread, p_source)
  ON CONFLICT (symbol, tf, ts_utc)
  DO UPDATE SET
    open = EXCLUDED.open,
    high = EXCLUDED.high,
    low = EXCLUDED.low,
    close = EXCLUDED.close,
    tick_volume = EXCLUDED.tick_volume,
    spread = EXCLUDED.spread,
    source_id = EXCLUDED.source_id,
    ingest_ts = NOW();
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION core.get_last_ts(
  p_symbol TEXT,
  p_tf core.tf_enum
) RETURNS TIMESTAMPTZ AS $$
DECLARE
  last_ts TIMESTAMPTZ;
BEGIN
  SELECT MAX(ts_utc) INTO last_ts
  FROM core.market_candles
  WHERE symbol = p_symbol AND tf = p_tf;
  RETURN last_ts;
END; $$ LANGUAGE plpgsql;

-- ============================================================================
-- FIN
-- ============================================================================
