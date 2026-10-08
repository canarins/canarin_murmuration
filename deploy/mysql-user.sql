-- Murmuration's own MySQL user. Read on the platform schemas, full on its own two.
-- Run as admin against canarin-mysql-prod; put the password in
-- Secrets Manager as canarin/db-murmuration-app-prod (plain string).
CREATE USER IF NOT EXISTS 'murmuration_app'@'%' IDENTIFIED BY '<from Secrets Manager>' REQUIRE SSL;
GRANT SELECT ON `Data`.*     TO 'murmuration_app'@'%';
GRANT SELECT ON `Devices`.*  TO 'murmuration_app'@'%';
GRANT SELECT ON `WebFront`.* TO 'murmuration_app'@'%';
GRANT ALL    ON `Murmuration`.*         TO 'murmuration_app'@'%';
GRANT ALL    ON `MurmurationInternal`.* TO 'murmuration_app'@'%';
-- Plumage reads the outputs; the internal schema is admin-only on the Plumage side.
GRANT SELECT ON `Murmuration`.*         TO 'plumage_app'@'%';
GRANT SELECT ON `MurmurationInternal`.* TO 'plumage_app'@'%';
FLUSH PRIVILEGES;
