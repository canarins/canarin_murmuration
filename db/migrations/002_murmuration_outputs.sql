-- 002 — The six relations Murmuration upserts into Plumage each cycle (paper §5.3).
-- Every row carries model_version and a credibility score in [0,1] + band.
--
-- PRIVACY SPLIT (hard rule): a public prediction names a pollutant at a place and
-- a time, never its source. field_forecast and virtual_sensor therefore carry NO
-- device_id and no contributor list. The four device-level relations live in the
-- murm_internal schema and must only ever be exposed to admins in Plumage.

CREATE SCHEMA IF NOT EXISTS murm_internal;

-- ---------------------------------------------------------------- public ----
CREATE TABLE IF NOT EXISTS field_forecast (
    model_version    text NOT NULL,
    issued_at        timestamptz NOT NULL,
    valid_at         timestamptz NOT NULL,
    cell_id          bigint NOT NULL,             -- output grid cell; lat/lon is its centre
    lat              double precision NOT NULL,
    lon              double precision NOT NULL,
    normalized_code  text NOT NULL,
    value            double precision NOT NULL,
    ci_low           double precision NOT NULL,
    ci_high          double precision NOT NULL,
    credibility      double precision NOT NULL CHECK (credibility BETWEEN 0 AND 1),
    band             text NOT NULL CHECK (band IN ('low', 'medium', 'high')),
    provisional      boolean NOT NULL DEFAULT false, -- served by the baseline under the floors
    PRIMARY KEY (model_version, issued_at, valid_at, cell_id, normalized_code)
);

CREATE TABLE IF NOT EXISTS virtual_sensor (
    model_version    text NOT NULL,
    ts               timestamptz NOT NULL,
    site_id          text NOT NULL,               -- query point, not a device
    lat              double precision NOT NULL,
    lon              double precision NOT NULL,
    normalized_code  text NOT NULL,
    value            double precision NOT NULL,
    ci_low           double precision NOT NULL,
    ci_high          double precision NOT NULL,
    credibility      double precision NOT NULL CHECK (credibility BETWEEN 0 AND 1),
    band             text NOT NULL CHECK (band IN ('low', 'medium', 'high')),
    PRIMARY KEY (model_version, ts, site_id, normalized_code)
);

CREATE TABLE IF NOT EXISTS virtual_sensor_site (
    site_id text PRIMARY KEY,
    lat     double precision NOT NULL,
    lon     double precision NOT NULL,
    label   text
);

-- ------------------------------------------------------------- internal ----
CREATE TABLE IF NOT EXISTS murm_internal.anomaly_score (
    model_version    text NOT NULL,
    device_id        text NOT NULL,
    instance_index   int  NOT NULL,
    normalized_code  text NOT NULL,
    ts               timestamptz NOT NULL,
    observed         double precision NOT NULL,
    expected         double precision NOT NULL,
    score            double precision NOT NULL,   -- standardized residual
    verdict          text NOT NULL,               -- healthy|fault|transient|gate:<name>
    credibility      double precision NOT NULL,
    PRIMARY KEY (model_version, device_id, instance_index, normalized_code, ts)
);

CREATE TABLE IF NOT EXISTS murm_internal.device_trust (
    model_version    text NOT NULL,
    device_id        text NOT NULL,
    normalized_code  text NOT NULL,               -- trust is per (device, channel)
    ts               timestamptz NOT NULL,
    trust            double precision NOT NULL CHECK (trust BETWEEN 0 AND 1),
    hard_fault_flag  boolean NOT NULL DEFAULT false,
    credibility      double precision NOT NULL,
    PRIMARY KEY (model_version, device_id, normalized_code, ts)
);

CREATE TABLE IF NOT EXISTS murm_internal.device_exposure (
    model_version    text NOT NULL,
    device_id        text NOT NULL,
    ts               timestamptz NOT NULL,
    exposure_coef    double precision NOT NULL CHECK (exposure_coef BETWEEN 0 AND 1),
    class            text NOT NULL CHECK (class IN ('outdoor', 'partial', 'indoor', 'personal')),
    credibility      double precision NOT NULL,
    PRIMARY KEY (model_version, device_id, ts)
);

CREATE TABLE IF NOT EXISTS murm_internal.device_position (
    model_version    text NOT NULL,
    device_id        text NOT NULL,
    lat              double precision NOT NULL,
    lon              double precision NOT NULL,
    uncertainty_m    double precision NOT NULL,
    method           text NOT NULL,               -- declared|survey|robust_median
    surveyed_at      timestamptz NOT NULL,
    credibility      double precision NOT NULL,
    PRIMARY KEY (model_version, device_id)
);

-- Hours touched by late data, for bounded reanalysis (never moves live state).
CREATE TABLE IF NOT EXISTS murm_internal.reanalysis_queue (
    normalized_code  text NOT NULL,
    ts_hour          timestamptz NOT NULL,
    n_late           int NOT NULL DEFAULT 0,
    first_seen       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (normalized_code, ts_hour)
);

-- Cycle log: one row per nowcast cycle, for ops dashboards.
CREATE TABLE IF NOT EXISTS murm_internal.cycle_log (
    cycle_id         bigserial PRIMARY KEY,
    model_version    text NOT NULL,
    started_at       timestamptz NOT NULL,
    finished_at      timestamptz,
    n_fresh          int, n_late int, n_stale int, n_skew int, n_gated int,
    model_used       text,                        -- baseline|core
    note             text
);
