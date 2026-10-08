"""Reads from and idempotent writes to the Canarin database (RDS MySQL 8.4).

Schemas: Devices / WebFront / Data (owned by Birdhouse + Plumage, read only here)
and Murmuration / MurmurationInternal (owned by this service).
All writes are INSERT ... ON DUPLICATE KEY UPDATE on composite keys, so a retried
or duplicated cycle cannot corrupt the store.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pymysql

from .devices import DeviceInfo
from .gates import CodeSpec

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"


def _naive_utc(dt: datetime) -> datetime:
    """MySQL DATETIME has no zone: store UTC, naive."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class Plumage:
    def __init__(self, host: str, port: int, user: str, password: str, ssl_ca: str | None = None):
        kw = dict(host=host, port=port, user=user, password=password, autocommit=True,
                  charset="utf8mb4", cursorclass=pymysql.cursors.Cursor)
        if ssl_ca:
            kw["ssl"] = {"ca": ssl_ca}
        self._kw = kw
        self.conn = pymysql.connect(**kw)

    def _ensure(self):
        """Reconnect after an idle timeout / RDS failover; the next cycle retries."""
        try:
            self.conn.ping(reconnect=False)
        except Exception:
            self.conn = pymysql.connect(**self._kw)

    def _q(self, sql: str, params=None) -> list[tuple]:
        self._ensure()
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _many(self, sql: str, rows: list[tuple]):
        if not rows:
            return
        self._ensure()
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)

    # ------------------------------------------------------------ schema --
    def migrate(self, include_stub: bool = True):
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if not include_stub and f.name.startswith("001_"):
                continue
            text = re.sub(r"--[^\n]*", "", f.read_text())
            with self.conn.cursor() as cur:
                for stmt in (s.strip() for s in text.split(";")):
                    if stmt:
                        cur.execute(stmt)

    # ------------------------------------------------------------- reads --
    def devices(self) -> dict[int, DeviceInfo]:
        """Enrolled, non-demo devices with a position; profile attributes if declared."""
        rows = self._q(
            "SELECT dh.id_internal, COALESCE(p.device_type,'other'), p.device_model, p.power_source, "
            "       COALESCE(p.declared_lat, dh.last_lat), COALESCE(p.declared_lon, dh.last_long), "
            "       COALESCE(p.position_source, 'gnss'), p.accuracy_m, dh.gps_hdop "
            "FROM Devices.devices_hardware dh "
            "LEFT JOIN Murmuration.device_profile p ON p.device_id = dh.id_internal "
            "WHERE COALESCE(dh.is_demo, 0) = 0 "
            "  AND COALESCE(p.declared_lat, dh.last_lat) IS NOT NULL "
            "  AND COALESCE(p.declared_lon, dh.last_long) IS NOT NULL")
        out = {}
        for (did, dtype, model, power, lat, lon, psrc, acc, hdop) in rows:
            if acc is None and hdop is not None and hdop > 0:
                acc = 5.0 * float(hdop)                 # crude HDOP -> metres
            out[int(did)] = DeviceInfo(int(did), dtype, model, power, float(lat), float(lon), psrc,
                                       None if acc is None else float(acc))
        return out

    def registry(self) -> dict[int, CodeSpec]:
        rows = self._q(
            "SELECT measure_type, code, role, decoder_head, cold_start_trust, sanity_min, sanity_max, "
            "parent_type, constraint_kind FROM Murmuration.measure_registry")
        return {int(r[0]): CodeSpec(int(r[0]), r[1], r[2], bool(r[3]), float(r[4]), r[5], r[6],
                                    None if r[7] is None else int(r[7]), r[8]) for r in rows}

    def hourly_history(self, mtype: int, since: datetime, until: datetime,
                       device_ids: list[int], instance_index: int = 1
                       ) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
        """[T, N] hourly mean matrix (NaN where missing) + hour-of-day + hour grid.

        Joins data_points_canonical to WebFront.pollutants to select the channel
        (measure type + instance) — that join is the real vocabulary.
        """
        rows = self._q(
            "SELECT c.device_id, DATE_FORMAT(c.time_bucket, '%%Y-%%m-%%d %%H:00:00'), AVG(c.value) "
            "FROM `Data`.data_points_canonical c "
            "JOIN WebFront.pollutants p ON p.id = c.pollutant_id "
            "WHERE p.pollutant_type_id = %s AND p.instance_index = %s "
            "  AND c.time_bucket >= %s AND c.time_bucket < %s "
            "GROUP BY 1, 2", (mtype, instance_index, _naive_utc(since), _naive_utc(until)))
        start = _aware(since).replace(minute=0, second=0, microsecond=0)
        T = max(1, int((_aware(until) - start).total_seconds() // 3600))
        grid = [start + timedelta(hours=i) for i in range(T)]
        col = {d: j for j, d in enumerate(device_ids)}
        S = np.full((T, len(device_ids)), np.nan)
        for d, h, v in rows:
            j = col.get(int(d))
            ht = datetime.strptime(h, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            i = int((ht - start).total_seconds() // 3600)
            if j is not None and 0 <= i < T:
                S[i, j] = float(v)
        return S, np.array([g.hour for g in grid]), grid

    def history_days(self, mtype: int, now: datetime) -> float:
        r = self._q(
            "SELECT MIN(c.time_bucket) FROM `Data`.data_points_canonical c "
            "JOIN WebFront.pollutants p ON p.id = c.pollutant_id "
            "WHERE p.pollutant_type_id = %s AND c.time_bucket <= %s", (mtype, _naive_utc(now)))
        return 0.0 if r[0][0] is None else (_aware(now) - _aware(r[0][0])).total_seconds() / 86400.0

    def virtual_sites(self) -> list[tuple[str, float, float]]:
        return [(s, float(a), float(b)) for s, a, b in
                self._q("SELECT site_id, lat, lon FROM Murmuration.virtual_sensor_site")]

    # ------------------------------------------------------------ writes --
    def _upsert(self, table: str, cols: list[str], key: list[str], rows: list[tuple]):
        upd = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in cols if c not in key)
        cols_sql = ", ".join(f"`{c}`" for c in cols)
        self._many(f"INSERT INTO {table} ({cols_sql}) VALUES ({', '.join(['%s'] * len(cols))}) "
                   f"ON DUPLICATE KEY UPDATE {upd}", rows)

    def write_forecasts(self, rows):
        cols = ["model_version", "issued_at", "valid_at", "cell_id", "lat", "lon", "measure_type", "code",
                "value", "ci_low", "ci_high", "credibility", "band", "provisional"]
        self._upsert("Murmuration.field_forecast", cols, cols[:4] + ["measure_type"],
                     [(r[0], _naive_utc(r[1]), _naive_utc(r[2]), *r[3:]) for r in rows])

    def write_virtual(self, rows):
        cols = ["model_version", "ts", "site_id", "lat", "lon", "measure_type", "code",
                "value", "ci_low", "ci_high", "credibility", "band"]
        self._upsert("Murmuration.virtual_sensor", cols, ["model_version", "ts", "site_id", "measure_type"],
                     [(r[0], _naive_utc(r[1]), *r[2:]) for r in rows])

    def write_anomalies(self, rows):
        cols = ["model_version", "device_id", "instance_index", "measure_type", "ts",
                "observed", "expected", "score", "verdict", "credibility"]
        self._upsert("MurmurationInternal.anomaly_score", cols, cols[:5],
                     [(*r[:4], _naive_utc(r[4]), *r[5:]) for r in rows])

    def write_trust(self, rows):
        cols = ["model_version", "device_id", "measure_type", "ts", "trust", "hard_fault_flag", "credibility"]
        self._upsert("MurmurationInternal.device_trust", cols, cols[:4],
                     [(*r[:3], _naive_utc(r[3]), r[4], int(bool(r[5])), r[6]) for r in rows])

    def write_exposure(self, rows):
        cols = ["model_version", "device_id", "ts", "exposure_coef", "class", "credibility"]
        self._upsert("MurmurationInternal.device_exposure", cols, cols[:3],
                     [(r[0], r[1], _naive_utc(r[2]), *r[3:]) for r in rows])

    def write_position(self, rows):
        cols = ["model_version", "device_id", "lat", "lon", "uncertainty_m", "method", "surveyed_at", "credibility"]
        self._upsert("MurmurationInternal.device_position", cols, cols[:2],
                     [(*r[:6], _naive_utc(r[6]), r[7]) for r in rows])

    def mark_late(self, counts: dict[tuple[int, datetime], int]):
        self._many(
            "INSERT INTO MurmurationInternal.reanalysis_queue (measure_type, ts_hour, n_late) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE n_late = n_late + VALUES(n_late)",
            [(c, _naive_utc(h), n) for (c, h), n in counts.items()])

    def log_cycle(self, **kw) -> None:
        cols = list(kw)
        vals = [_naive_utc(v) if isinstance(v, datetime) else v for v in kw.values()]
        self._many(f"INSERT INTO MurmurationInternal.cycle_log ({', '.join(cols)}) VALUES "
                   f"({', '.join(['%s'] * len(cols))})", [tuple(vals)])

    # ---------------------------------------------------------- dev only --
    def exec(self, sql: str, params=None):
        return self._q(sql, params)


def connect_from_settings(s) -> Plumage:
    return Plumage(s.db_host, s.db_port, s.db_user, s.db_pass, s.db_ssl_ca or None)


def dsn_from_env() -> str:  # kept for scripts that print a hint
    return f"mysql://{os.environ.get('DB_USER', 'plumage')}@{os.environ.get('DB_HOST', 'localhost')}"
