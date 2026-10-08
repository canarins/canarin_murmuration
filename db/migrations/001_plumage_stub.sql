-- 001 — LOCAL DEV / TEST ONLY: minimal stand-in for the Canarin tables Murmuration
-- reads. In production these exist already (RDS MySQL 8.4, owned by Birdhouse /
-- Plumage) and this file must NOT be applied (`murmuration migrate --outputs-only`).
-- Column names and schemas follow the live platform:
--   Devices.devices_hardware, WebFront.pollutants, WebFront.pollutant_types,
--   Data.data_points_canonical  (see canarins_birdhouse/canonical_ingest.py)

CREATE DATABASE IF NOT EXISTS Devices;
CREATE DATABASE IF NOT EXISTS WebFront;
CREATE DATABASE IF NOT EXISTS `Data`;

CREATE TABLE IF NOT EXISTS Devices.devices_hardware (
    id_internal    INT PRIMARY KEY,
    id_native      VARCHAR(64),
    Sensor_key     VARCHAR(64),
    project_id     INT,
    is_demo        TINYINT DEFAULT 0,
    last_lat       DOUBLE,
    last_long      DOUBLE,
    gps_hdop       DOUBLE,
    gps_satellites INT
);

CREATE TABLE IF NOT EXISTS WebFront.pollutant_types (
    id            INT PRIMARY KEY,
    code          VARCHAR(32) NOT NULL,
    min_plausible DOUBLE,
    max_plausible DOUBLE
);

-- per-device stream declaration (device_id, legacy_value_id) -> measure type
CREATE TABLE IF NOT EXISTS WebFront.pollutants (
    id                INT AUTO_INCREMENT PRIMARY KEY,
    device_id         INT NOT NULL,
    pollutant_type_id INT NOT NULL,
    granularity       INT NOT NULL DEFAULT 60,
    instance_index    INT NOT NULL DEFAULT 1,
    instance_label    VARCHAR(64),
    scale_factor      DOUBLE NOT NULL DEFAULT 1.0,
    legacy_value_id   INT,
    note              VARCHAR(255),
    UNIQUE KEY uq_device_type_gran_idx (device_id, pollutant_type_id, granularity, instance_index)
);

CREATE TABLE IF NOT EXISTS `Data`.data_points_canonical (
    device_id    INT NOT NULL,
    time_bucket  DATETIME NOT NULL,
    pollutant_id INT NOT NULL,
    value        DOUBLE NOT NULL,
    raw_value    DOUBLE,
    PRIMARY KEY (device_id, time_bucket, pollutant_id),
    KEY idx_pollutant_time (pollutant_id, time_bucket)
);

INSERT IGNORE INTO WebFront.pollutant_types (id, code, min_plausible, max_plausible) VALUES
  (1, 'PM1', 0, 2000), (2, 'PM2.5', 0, 2000), (3, 'PM10', 0, 2000),
  (9, 'CO2', 0, 10000), (13, 'TVOC', 0, 60000), (16, 'temperature', -40, 125),
  (17, 'humidity', 0, 100), (18, 'pressure', 800, 1100), (863, 'battery', 0, 6000);
