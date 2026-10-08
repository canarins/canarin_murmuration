"""Score a finished `murmuration simulate` run against the synthetic truth.

Prints: trust by device, verdict counts, late data, forecast skill vs persistence
at the cells holding healthy Picos, and 90% band coverage.
Run after scripts/reset_local_db.sh && murmuration simulate.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import timedelta

import numpy as np

from murmuration.config import Settings
from murmuration.geo import cell_id
from murmuration.plumage import connect_from_settings

PM25 = 2


def main():
    s = Settings()
    db = connect_from_settings(s)
    dev = {int(r[0]): (r[1], r[2], float(r[3]), float(r[4])) for r in db.exec(
        "SELECT dh.id_internal, p.device_type, p.power_source, dh.last_lat, dh.last_long "
        "FROM Devices.devices_hardware dh LEFT JOIN Murmuration.device_profile p ON p.device_id = dh.id_internal")}

    print("== final trust (PM2.5) ==")
    rows = db.exec("""SELECT t.device_id, t.trust, t.hard_fault_flag FROM MurmurationInternal.device_trust t
                      JOIN (SELECT device_id, MAX(ts) ts FROM MurmurationInternal.device_trust
                            WHERE measure_type=%s GROUP BY device_id) m
                        ON m.device_id=t.device_id AND m.ts=t.ts WHERE t.measure_type=%s ORDER BY 1""", (PM25, PM25))
    for d, t, h in rows:
        print(f"  {d:6d} {dev[d][0]:6s} {t:5.2f} {'HARD' if h else ''}")

    print("\n== verdicts (PM2.5, count) ==")
    for d, v, n in db.exec("""SELECT device_id, verdict, COUNT(*) FROM MurmurationInternal.anomaly_score
                              WHERE measure_type=%s GROUP BY 1,2 ORDER BY 1,2""", (PM25,)):
        if v != "healthy":
            print(f"  {d:6d} {v:16s} {n}")

    print("\n== late data (hours marked, readings) ==")
    print("  ", db.exec("SELECT COUNT(*), SUM(n_late) FROM MurmurationInternal.reanalysis_queue")[0])

    step = s.grid_step_deg
    healthy = [d for d, (t, p, la, lo) in dev.items() if t == "pico" and d not in (1001, 1002)]
    truth = defaultdict(dict)
    for d, h, v in db.exec("""SELECT c.device_id, DATE_FORMAT(c.time_bucket,'%%Y-%%m-%%d %%H:00:00'), AVG(c.value)
                              FROM `Data`.data_points_canonical c JOIN WebFront.pollutants p ON p.id=c.pollutant_id
                              WHERE p.pollutant_type_id=%s GROUP BY 1,2""", (PM25,)):
        truth[int(d)][h] = float(v)
    err, pers, cov = defaultdict(list), defaultdict(list), defaultdict(list)
    for d in healthy:
        cid = cell_id(dev[d][2], dev[d][3], step)
        for issued, valid, val, lo, hi in db.exec(
                """SELECT issued_at, valid_at, value, ci_low, ci_high FROM Murmuration.field_forecast
                   WHERE measure_type=%s AND cell_id=%s AND MINUTE(issued_at)=0""", (PM25, cid)):
            base = issued.replace(minute=0, second=0, microsecond=0)
            h = round((valid - base).total_seconds() / 3600)
            y = truth[d].get(valid.strftime("%Y-%m-%d %H:%M:%S"))
            y0 = truth[d].get((base - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))
            if y is None or y0 is None:
                continue
            err[h].append(val - y); pers[h].append(y0 - y); cov[h].append(lo <= y <= hi)
    print("\n== forecast at healthy-pico cells (PM2.5, hourly issues) ==")
    print("   h   n   RMSE   persist  skill   cov90")
    for h in sorted(err):
        e, p = np.array(err[h]), np.array(pers[h])
        r, rp = np.sqrt(np.mean(e ** 2)), np.sqrt(np.mean(p ** 2))
        print(f"  {h:2d} {len(e):4d} {r:6.2f} {rp:8.2f} {1 - r / rp:+6.1%} {np.mean(cov[h]):6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
