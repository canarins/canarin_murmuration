"""Synthetic Paris fleet for local development and tests.

Port of baseline.make_synthetic to lat/lon at 5-minute resolution: a diurnal
base with two rush-hour peaks, a source plume advected by a slowly rotating
wind, AR(1) sensor noise — plus injectable faults (stuck, drift, indoor) and
two Femto archetypes (mains = indoor, phone = personal exposure).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from .queue import Record

PARIS = (48.8566, 2.3522)
STEP_MIN = 5


MT_PM25, MT_PM10 = 2, 3           # WebFront.pollutant_types ids
CHANNEL_ID = {}                  # (device_id, mtype) -> WebFront.pollutants.id, filled by seed_plumage


@dataclass
class SynthDevice:
    device_id: int
    device_type: str
    power_source: str
    lat: float
    lon: float
    fault: str | None = None          # stuck | drift | None
    fault_at_step: int = 0


@dataclass
class SynthFleet:
    start: datetime
    devices: list[SynthDevice]
    pm25: np.ndarray                  # [steps, N]
    sites: list[tuple[str, float, float]] = field(default_factory=list)

    def step_of(self, t: datetime) -> int:
        return int((t - self.start).total_seconds() // (STEP_MIN * 60))

    def time_of(self, k: int) -> datetime:
        return self.start + timedelta(minutes=STEP_MIN * k)

    def records_at(self, k: int, received_at: datetime | None = None) -> list[Record]:
        ts = self.time_of(k)
        ra = received_at or ts
        out = []
        for j, d in enumerate(self.devices):
            v = float(self.pm25[k, j])
            out.append(Record(d.device_id, 1, MT_PM25, ts, v, ra))
            out.append(Record(d.device_id, 1, MT_PM10, ts, v * 1.45 + 0.5, ra))
        return out


def make_fleet(n_pico: int = 14, days_hist: int = 21, days_live: int = 2, seed: int = 7,
               start: datetime | None = None, faults: bool = True) -> SynthFleet:
    rng = np.random.default_rng(seed)
    start = start or datetime(2026, 9, 1, tzinfo=timezone.utc)
    steps = (days_hist + days_live) * 24 * 60 // STEP_MIN

    devs = []
    for i in range(n_pico):
        lat = PARIS[0] + rng.uniform(-0.03, 0.03)
        lon = PARIS[1] + rng.uniform(-0.045, 0.045)
        devs.append(SynthDevice(1000 + i, "pico", "mains", lat, lon))
    devs.append(SynthDevice(2001, "femto", "mains", PARIS[0] + 0.004, PARIS[1] - 0.006))   # indoor
    devs.append(SynthDevice(2002, "femto", "phone", PARIS[0] - 0.006, PARIS[1] + 0.008))   # personal
    if faults and n_pico >= 4:
        live0 = days_hist * 24 * 60 // STEP_MIN
        devs[1].fault, devs[1].fault_at_step = "stuck", live0 - 12 * 6     # 6 h before live window
        devs[2].fault, devs[2].fault_at_step = "drift", live0 - 12 * 24    # a day before

    N = len(devs)
    lat0, lon0 = PARIS
    pos = np.array([[(d.lon - lon0) * 111.32 * math.cos(math.radians(lat0)),
                     (d.lat - lat0) * 110.57] for d in devs])
    t_h = np.arange(steps) * STEP_MIN / 60.0
    hour = t_h % 24
    diurnal = 12.0 * (1.0 + 0.6 * np.exp(-((hour - 8) ** 2) / 6) + 0.7 * np.exp(-((hour - 19) ** 2) / 8))
    ang = 0.03 * t_h + 0.5 * np.sin(0.01 * t_h)
    src = np.array([0.5, -0.5])
    S = np.zeros((steps, N))
    ar = np.zeros(N)
    for k in range(steps):
        d = pos - src
        ca, sa = math.cos(ang[k]), math.sin(ang[k])
        along = d[:, 0] * ca + d[:, 1] * sa
        cross = -d[:, 0] * sa + d[:, 1] * ca
        plume = 1.2 * np.exp(-(cross ** 2) / 1.5) * np.exp(-np.abs(along) / 4)
        ar = 0.97 * ar + rng.normal(0, 0.35, N)
        S[k] = diurnal[k] * (1.0 + plume) + ar + rng.normal(0, 0.5, N)

    for j, d in enumerate(devs):
        if d.device_type == "femto" and d.power_source == "mains":     # indoor: buffered, own cycle
            smooth = np.convolve(S[:, j], np.ones(72) / 72, mode="same")
            S[:, j] = 0.4 * smooth + 6 + 8 * np.exp(-((hour - 20) ** 2) / 0.5)   # cooking spike
        if d.device_type == "femto" and d.power_source == "phone":     # carried around
            S[:, j] = S[:, j] * (1 + 0.6 * np.sin(t_h / 3.0) ** 2)
        if d.fault == "stuck":
            S[d.fault_at_step:, j] = S[d.fault_at_step, j]
        elif d.fault == "drift":
            n = steps - d.fault_at_step
            S[d.fault_at_step:, j] += np.linspace(0, 0.4 * n * STEP_MIN / 60, n)
    S = np.clip(S, 0, None)

    sites = [("vs-chatelet", 48.8584, 2.3470), ("vs-bastille", 48.8532, 2.3692),
             ("vs-montmartre", 48.8867, 2.3431)]
    return SynthFleet(start, devs, S, sites)


def seed_plumage(db, fleet: SynthFleet, until_step: int):
    """Write devices (hardware + profile), channel declarations, virtual sites and
    the historical canonical readings up to `until_step` (exclusive)."""
    for d in fleet.devices:
        db.exec("INSERT IGNORE INTO Devices.devices_hardware (id_internal, id_native, is_demo, last_lat, last_long, "
                "gps_hdop, gps_satellites) VALUES (%s,%s,0,%s,%s,%s,%s)",
                (d.device_id, f"synth-{d.device_id}", d.lat, d.lon,
                 1.2 if d.device_type == "pico" else None, 9 if d.device_type == "pico" else None))
        db.exec("INSERT IGNORE INTO Murmuration.device_profile (device_id, device_type, device_model, power_source, "
                "position_source) VALUES (%s,%s,%s,%s,%s)",
                (d.device_id, d.device_type, f"{d.device_type}-r1", d.power_source,
                 "gnss" if d.device_type == "pico" else ("phone" if d.power_source == "phone" else "wifi")))
        for pt, legacy in ((MT_PM25, 7), (MT_PM10, 8)):
            db.exec("INSERT IGNORE INTO WebFront.pollutants (device_id, pollutant_type_id, granularity, instance_index, "
                    "scale_factor, legacy_value_id) VALUES (%s,%s,60,1,1.0,%s)", (d.device_id, pt, legacy))
    for (pid, did, pt) in db.exec("SELECT id, device_id, pollutant_type_id FROM WebFront.pollutants"):
        CHANNEL_ID[(int(did), int(pt))] = int(pid)
    for sid, la, lo in fleet.sites:
        db.exec("INSERT IGNORE INTO Murmuration.virtual_sensor_site (site_id, lat, lon) VALUES (%s,%s,%s)", (sid, la, lo))
    rows = []
    for k in range(until_step):
        for r in fleet.records_at(k):
            rows.append((r.device_id, r.ts.replace(tzinfo=None), CHANNEL_ID[(r.device_id, r.mtype)], r.value, r.value))
    for i in range(0, len(rows), 5000):
        db._many("INSERT IGNORE INTO `Data`.data_points_canonical (device_id, time_bucket, pollutant_id, value, raw_value) "
                 "VALUES (%s,%s,%s,%s,%s)", rows[i:i + 5000])


def store_records(db, records: list[Record], received_at: datetime | None = None):
    """What Birdhouse does on the hot path: idempotent canonical insert (then XADD)."""
    db._many("INSERT IGNORE INTO `Data`.data_points_canonical (device_id, time_bucket, pollutant_id, value, raw_value) "
             "VALUES (%s,%s,%s,%s,%s)",
             [(r.device_id, r.ts.replace(tzinfo=None), CHANNEL_ID[(r.device_id, r.mtype)], r.value, r.value)
              for r in records])
