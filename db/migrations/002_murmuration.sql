-- 002 — Murmuration's own tables (RDS MySQL 8.4). Safe to apply in production:
-- it only creates the two Murmuration schemas, never touches Data/Devices/WebFront.
--
-- PRIVACY SPLIT (hard rule): a public prediction names a measure at a place and
-- a time, never its source. `Murmuration` (public) tables carry NO device_id and
-- no contributor list. `MurmurationInternal` holds the device-level relations and
-- must only ever be exposed to admins in Plumage — grant it to plumage_app's admin
-- role only.
--
-- Measures are identified by WebFront.pollutant_types.id (`measure_type`);
-- `code` is denormalized on output rows for readability (e.g. 'PM2.5').

CREATE DATABASE IF NOT EXISTS Murmuration;
CREATE DATABASE IF NOT EXISTS MurmurationInternal;

-- ---------------------------------------------------------------- config ----
-- What the model does with each measure type (paper §3 "the types table").
-- Enrolling a new channel is a row insert, not a code change.
CREATE TABLE IF NOT EXISTS Murmuration.measure_registry (
    measure_type   INT PRIMARY KEY,                 -- WebFront.pollutant_types.id
    code             VARCHAR(32) NOT NULL,
    block            ENUM('geo','hardware','weather','sensing') NOT NULL,
    role             ENUM('target','covariate','diagnostic','derived','housekeeping','exogenous') NOT NULL,
    decoder_head     TINYINT NOT NULL DEFAULT 0,
    tranche_eligible TINYINT NOT NULL DEFAULT 0,
    cold_start_trust DOUBLE NOT NULL DEFAULT 0.5,
    sanity_min       DOUBLE,
    sanity_max       DOUBLE,
    parent_type      INT,                             -- nested fraction parent (PM1 -> PM2.5 -> PM10)
    constraint_kind  ENUM('le_parent')
);

-- Per-device attributes the platform does not hold yet (paper Table 4). Device
-- type and power source are DECLARED, not learned; they seed the exposure and
-- position priors. Rows are optional: an unknown device gets type 'other'.
CREATE TABLE IF NOT EXISTS Murmuration.device_profile (
    device_id        INT PRIMARY KEY,                 -- Devices.devices_hardware.id_internal
    device_type      ENUM('pico','femto','other') NOT NULL DEFAULT 'other',
    device_model     VARCHAR(64),                     -- board/sensor revision (hierarchy level 2)
    power_source     ENUM('mains','battery','phone'),
    position_source  ENUM('gnss','phone','wifi','declared'),
    declared_lat     DOUBLE,                          -- overrides devices_hardware.last_lat when set
    declared_lon     DOUBLE,
    accuracy_m       DOUBLE
);

CREATE TABLE IF NOT EXISTS Murmuration.virtual_sensor_site (
    site_id  VARCHAR(64) PRIMARY KEY,
    lat      DOUBLE NOT NULL,
    lon      DOUBLE NOT NULL,
    label    VARCHAR(128)
);

-- ---------------------------------------------------------------- public ----
CREATE TABLE IF NOT EXISTS Murmuration.field_forecast (
    model_version    VARCHAR(64) NOT NULL,
    issued_at        DATETIME NOT NULL,
    valid_at         DATETIME NOT NULL,
    cell_id          BIGINT NOT NULL,                 -- output grid cell; lat/lon is its centre
    lat              DOUBLE NOT NULL,
    lon              DOUBLE NOT NULL,
    measure_type   INT NOT NULL,
    code             VARCHAR(32) NOT NULL,
    value            DOUBLE NOT NULL,
    ci_low           DOUBLE NOT NULL,
    ci_high          DOUBLE NOT NULL,
    credibility      DOUBLE NOT NULL,
    band             ENUM('low','medium','high') NOT NULL,
    provisional      TINYINT NOT NULL DEFAULT 0,      -- under the floors: baseline-grade output
    PRIMARY KEY (model_version, issued_at, valid_at, cell_id, measure_type),
    KEY idx_valid (measure_type, valid_at)
);

CREATE TABLE IF NOT EXISTS Murmuration.virtual_sensor (
    model_version    VARCHAR(64) NOT NULL,
    ts               DATETIME NOT NULL,
    site_id          VARCHAR(64) NOT NULL,            -- a query point, not a device
    lat              DOUBLE NOT NULL,
    lon              DOUBLE NOT NULL,
    measure_type   INT NOT NULL,
    code             VARCHAR(32) NOT NULL,
    value            DOUBLE NOT NULL,
    ci_low           DOUBLE NOT NULL,
    ci_high          DOUBLE NOT NULL,
    credibility      DOUBLE NOT NULL,
    band             ENUM('low','medium','high') NOT NULL,
    PRIMARY KEY (model_version, ts, site_id, measure_type)
);

-- -------------------------------------------------------------- internal ----
CREATE TABLE IF NOT EXISTS MurmurationInternal.anomaly_score (
    model_version    VARCHAR(64) NOT NULL,
    device_id        INT NOT NULL,
    instance_index   INT NOT NULL,
    measure_type   INT NOT NULL,
    ts               DATETIME NOT NULL,
    observed         DOUBLE NOT NULL,
    expected         DOUBLE NOT NULL,
    score            DOUBLE NOT NULL,                 -- standardized residual
    verdict          VARCHAR(32) NOT NULL,            -- healthy|fault|transient|unjudged|gate:<name>
    credibility      DOUBLE NOT NULL,
    PRIMARY KEY (model_version, device_id, instance_index, measure_type, ts)
);

CREATE TABLE IF NOT EXISTS MurmurationInternal.device_trust (
    model_version    VARCHAR(64) NOT NULL,
    device_id        INT NOT NULL,
    measure_type   INT NOT NULL,                    -- trust is per (device, channel)
    ts               DATETIME NOT NULL,
    trust            DOUBLE NOT NULL,
    hard_fault_flag  TINYINT NOT NULL DEFAULT 0,
    credibility      DOUBLE NOT NULL,
    PRIMARY KEY (model_version, device_id, measure_type, ts)
);

CREATE TABLE IF NOT EXISTS MurmurationInternal.device_exposure (
    model_version    VARCHAR(64) NOT NULL,
    device_id        INT NOT NULL,
    ts               DATETIME NOT NULL,
    exposure_coef    DOUBLE NOT NULL,
    class            ENUM('outdoor','partial','indoor','personal') NOT NULL,
    credibility      DOUBLE NOT NULL,
    PRIMARY KEY (model_version, device_id, ts)
);

CREATE TABLE IF NOT EXISTS MurmurationInternal.device_position (
    model_version    VARCHAR(64) NOT NULL,
    device_id        INT NOT NULL,
    lat              DOUBLE NOT NULL,
    lon              DOUBLE NOT NULL,
    uncertainty_m    DOUBLE NOT NULL,
    method           VARCHAR(32) NOT NULL,            -- declared|survey|robust_median
    surveyed_at      DATETIME NOT NULL,
    credibility      DOUBLE NOT NULL,
    PRIMARY KEY (model_version, device_id)
);

-- Hours touched by late data, for bounded reanalysis (late data never moves live state).
CREATE TABLE IF NOT EXISTS MurmurationInternal.reanalysis_queue (
    measure_type   INT NOT NULL,
    ts_hour          DATETIME NOT NULL,
    n_late           INT NOT NULL DEFAULT 0,
    first_seen       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (measure_type, ts_hour)
);

CREATE TABLE IF NOT EXISTS MurmurationInternal.cycle_log (
    cycle_id         BIGINT AUTO_INCREMENT PRIMARY KEY,
    model_version    VARCHAR(64) NOT NULL,
    started_at       DATETIME NOT NULL,
    finished_at      DATETIME,
    n_fresh INT, n_late INT, n_stale INT, n_skew INT, n_gated INT,
    model_used       VARCHAR(32),
    note             VARCHAR(255)
);

-- Seed the registry with the fleet's channels (ids from WebFront.pollutant_types).
INSERT IGNORE INTO Murmuration.measure_registry
  (measure_type, code, block, role, decoder_head, tranche_eligible, cold_start_trust, sanity_min, sanity_max, parent_type, constraint_kind) VALUES
  (3,   'PM10',        'sensing',  'target',       1, 1, 0.6, 0, 2000, NULL, NULL),
  (2,   'PM2.5',       'sensing',  'target',       1, 1, 0.6, 0, 2000, 3,    'le_parent'),
  (1,   'PM1',         'sensing',  'target',       1, 1, 0.6, 0, 2000, 2,    'le_parent'),
  (13,  'TVOC',        'sensing',  'target',       1, 1, 0.5, 0, 60000, NULL, NULL),
  (9,   'CO2',         'sensing',  'derived',      0, 0, 0.3, 0, 10000, NULL, NULL),
  (17,  'humidity',    'sensing',  'covariate',    0, 0, 0.6, 0, 100,   NULL, NULL),
  (18,  'pressure',    'sensing',  'covariate',    0, 0, 0.6, 800, 1100, NULL, NULL),
  (16,  'temperature', 'hardware', 'housekeeping', 0, 0, 0.5, -40, 125, NULL, NULL),   -- device self-heat, never ambient
  (863, 'battery',     'hardware', 'housekeeping', 0, 0, 0.5, 0, 6000,  NULL, NULL);
