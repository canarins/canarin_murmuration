"""Reads from and idempotent writes to Plumage (Postgres).

All writes are upserts on composite keys, so a retried or duplicated cycle
cannot corrupt the store.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import psycopg

from .devices import DeviceInfo
from .gates import CodeSpec

MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"


class Plumage:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, autocommit=True)

    # ------------------------------------------------------------ schema --
    def migrate(self, include_stub: bool = True):
        for f in sorted(MIGRATIONS.glob("*.sql")):
            if not include_stub and f.name.startswith("001_"):
                continue
            self.conn.execute(f.read_text())

    # ------------------------------------------------------------- reads --
    def devices(self) -> dict[str, DeviceInfo]:
        rows = self.conn.execute(
            "SELECT device_id, device_type, device_model, power_source, lat, lon, "
            "position_source, accuracy_m FROM devices WHERE lat IS NOT NULL").fetchall()
        return {r[0]: DeviceInfo(*r) for r in rows}

    def registry(self) -> dict[str, CodeSpec]:
        rows = self.conn.execute(
            "SELECT normalized_code, role, decoder_head, cold_start_trust, sanity_min, "
            "sanity_max, parent_code, constraint_kind FROM code_registry").fetchall()
        return {r[0]: CodeSpec(*r) for r in rows}

    def hourly_history(self, code: str, since: datetime, until: datetime,
                       device_ids: list[str]) -> tuple[np.ndarray, np.ndarray, list[datetime]]:
        """[T, N] hourly mean matrix (NaN where missing) + hour-of-day + hour grid."""
        rows = self.conn.execute(
            "SELECT device_id, date_trunc('hour', ts) AS h, avg(value) FROM readings "
            "WHERE normalized_code = %s AND ts >= %s AND ts < %s AND instance_index = 0 "
            "GROUP BY 1, 2", (code, since, until)).fetchall()
        start = since.replace(minute=0, second=0, microsecond=0)
        T = max(1, int((until - start).total_seconds() // 3600))
        grid = [start + timedelta(hours=i) for i in range(T)]
        col = {d: j for j, d in enumerate(device_ids)}
        S = np.full((T, len(device_ids)), np.nan)
        for d, h, v in rows:
            j = col.get(d)
            i = int((h - start).total_seconds() // 3600)
            if j is not None and 0 <= i < T:
                S[i, j] = v
        hours = np.array([g.hour for g in grid])
        return S, hours, grid

    def history_days(self, code: str, now: datetime) -> float:
        r = self.conn.execute("SELECT min(ts) FROM readings WHERE normalized_code = %s AND ts <= %s",
                              (code, now)).fetchone()
        return 0.0 if r[0] is None else (now - r[0]).total_seconds() / 86400.0

    def virtual_sites(self) -> list[tuple[str, float, float]]:
        return self.conn.execute("SELECT site_id, lat, lon FROM virtual_sensor_site").fetchall()

    # ------------------------------------------------------------ writes --
    def _upsert(self, table: str, cols: list[str], key: list[str], rows: list[tuple]):
        if not rows:
            return
        upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in key)
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
               f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {upd}")
        with self.conn.cursor() as cur:
            cur.executemany(sql, rows)

    def write_forecasts(self, rows):
        cols = ["model_version", "issued_at", "valid_at", "cell_id", "lat", "lon", "normalized_code",
                "value", "ci_low", "ci_high", "credibility", "band", "provisional"]
        self._upsert("field_forecast", cols, cols[:4] + ["normalized_code"], rows)

    def write_virtual(self, rows):
        cols = ["model_version", "ts", "site_id", "lat", "lon", "normalized_code",
                "value", "ci_low", "ci_high", "credibility", "band"]
        self._upsert("virtual_sensor", cols, ["model_version", "ts", "site_id", "normalized_code"], rows)

    def write_anomalies(self, rows):
        cols = ["model_version", "device_id", "instance_index", "normalized_code", "ts",
                "observed", "expected", "score", "verdict", "credibility"]
        self._upsert("murm_internal.anomaly_score", cols, cols[:5], rows)

    def write_trust(self, rows):
        cols = ["model_version", "device_id", "normalized_code", "ts", "trust", "hard_fault_flag",
                "credibility"]
        self._upsert("murm_internal.device_trust", cols, cols[:4], rows)

    def write_exposure(self, rows):
        cols = ["model_version", "device_id", "ts", "exposure_coef", "class", "credibility"]
        self._upsert("murm_internal.device_exposure", cols, cols[:3], rows)

    def write_position(self, rows):
        cols = ["model_version", "device_id", "lat", "lon", "uncertainty_m", "method", "surveyed_at",
                "credibility"]
        self._upsert("murm_internal.device_position", cols, cols[:2], rows)

    def mark_late(self, counts: dict[tuple[str, datetime], int]):
        if not counts:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO murm_internal.reanalysis_queue (normalized_code, ts_hour, n_late) "
                "VALUES (%s, %s, %s) ON CONFLICT (normalized_code, ts_hour) "
                "DO UPDATE SET n_late = murm_internal.reanalysis_queue.n_late + EXCLUDED.n_late",
                [(c, h, n) for (c, h), n in counts.items()])

    def log_cycle(self, **kw) -> None:
        cols = list(kw)
        self.conn.execute(
            f"INSERT INTO murm_internal.cycle_log ({', '.join(cols)}) VALUES "
            f"({', '.join(['%s'] * len(cols))})", list(kw.values()))


def dsn_from_env() -> str:
    return os.environ.get("MURM_PG_DSN", "postgresql://plumage:plumage@localhost:5432/plumage")
