-- 001 — Minimal stand-in for the Plumage tables Murmuration reads.
-- In production these already exist (owned by Birdhouse/Plumage); this file is
-- only for local development and tests. Column names follow the normalized
-- vocabulary established at ingestion (scale_factor / normalized_code / instance_index).

CREATE TABLE IF NOT EXISTS devices (
    device_id        text PRIMARY KEY,
    device_type      text NOT NULL CHECK (device_type IN ('pico', 'femto', 'other')),
    device_model     text,                       -- board/sensor revision (hierarchy level 2)
    power_source     text CHECK (power_source IN ('mains', 'battery', 'phone')),
    lat              double precision,
    lon              double precision,
    position_source  text,                       -- gnss | phone | wifi | declared
    accuracy_m       double precision,           -- reported horizontal accuracy / HDOP-derived
    enrolled_at      timestamptz NOT NULL DEFAULT now()
);

-- Registry of normalized codes: block + role drive what the model does with each code.
-- Enrolling a new sensor channel is a row insert, not a code change.
CREATE TABLE IF NOT EXISTS code_registry (
    normalized_code  text PRIMARY KEY,
    block            text NOT NULL CHECK (block IN ('geo', 'hardware', 'weather', 'sensing')),
    role             text NOT NULL CHECK (role IN ('target', 'covariate', 'diagnostic',
                                                   'derived', 'housekeeping', 'exogenous')),
    decoder_head     boolean NOT NULL DEFAULT false,
    tranche_eligible boolean NOT NULL DEFAULT false,
    cold_start_trust double precision NOT NULL DEFAULT 0.5,
    sanity_min       double precision,
    sanity_max       double precision,
    parent_code      text REFERENCES code_registry(normalized_code),
    constraint_kind  text CHECK (constraint_kind IN ('le_parent'))
);

-- Weather is stored once per (cell, hour) — never per device (paper §3).
CREATE TABLE IF NOT EXISTS weather_cell (
    cell_id          bigint NOT NULL,
    ts_hour          timestamptz NOT NULL,
    normalized_code  text NOT NULL,
    value            double precision NOT NULL,
    wx_stale         boolean NOT NULL DEFAULT false,
    PRIMARY KEY (cell_id, ts_hour, normalized_code)
);

CREATE TABLE IF NOT EXISTS readings (
    device_id        text NOT NULL REFERENCES devices(device_id),
    instance_index   int  NOT NULL DEFAULT 0,
    normalized_code  text NOT NULL,
    ts               timestamptz NOT NULL,        -- device-stamped event time
    value            double precision NOT NULL,   -- already scale_factor-normalized
    received_at      timestamptz NOT NULL DEFAULT now(),
    weather_cell_id  bigint,
    PRIMARY KEY (device_id, instance_index, normalized_code, ts)
);
CREATE INDEX IF NOT EXISTS readings_code_ts ON readings (normalized_code, ts);

INSERT INTO code_registry (normalized_code, block, role, decoder_head, tranche_eligible,
                           cold_start_trust, sanity_min, sanity_max) VALUES
  ('pm10',     'sensing',  'target',       true,  true,  0.6, 0, 2000),
  ('voc_index','sensing',  'target',       true,  true,  0.5, 0, 500),
  ('eco2',     'sensing',  'derived',      false, false, 0.3, 0, 10000),
  ('humidity', 'sensing',  'covariate',    false, false, 0.6, 0, 100),
  ('pressure', 'sensing',  'covariate',    false, false, 0.6, 800, 1100),
  ('board_temp','hardware','housekeeping', false, false, 0.5, -40, 125),
  ('battery',  'hardware', 'housekeeping', false, false, 0.5, 0, 100),
  ('wind_u',   'weather',  'exogenous',    false, false, 1.0, -60, 60),
  ('wind_v',   'weather',  'exogenous',    false, false, 1.0, -60, 60),
  ('air_temp', 'weather',  'exogenous',    false, false, 1.0, -50, 60)
ON CONFLICT DO NOTHING;
INSERT INTO code_registry (normalized_code, block, role, decoder_head, tranche_eligible,
                           cold_start_trust, sanity_min, sanity_max, parent_code, constraint_kind) VALUES
  ('pm25', 'sensing', 'target', true, true, 0.6, 0, 2000, 'pm10', 'le_parent')
ON CONFLICT DO NOTHING;
INSERT INTO code_registry (normalized_code, block, role, decoder_head, tranche_eligible,
                           cold_start_trust, sanity_min, sanity_max, parent_code, constraint_kind) VALUES
  ('pm1',  'sensing', 'target', true, true, 0.6, 0, 2000, 'pm25', 'le_parent')
ON CONFLICT DO NOTHING;
