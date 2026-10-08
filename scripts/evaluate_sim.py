"""Score a finished `murmuration simulate` run against the synthetic truth.

Prints: trust by device category, effect of the 6 h outage + late flush on the
dark device's trust, forecast skill vs persistence at device cells, and
90% band coverage. Run after scripts/reset_local_db.sh && murmuration simulate.
"""
from __future__ import annotations

import sys
from collections import defaultdict

import numpy as np
import psycopg

from murmuration.config import Settings
from murmuration.geo import cell_id


def main():
    s = Settings()
    c = psycopg.connect(s.pg_dsn)
    dev = {r[0]: r[1:] for r in c.execute("SELECT device_id, device_type, power_source, lat, lon FROM devices")}

    print("== final trust (pm25) ==")
    rows = c.execute("""SELECT DISTINCT ON (device_id) device_id, trust, hard_fault_flag
                        FROM murm_internal.device_trust WHERE normalized_code='pm25'
                        ORDER BY device_id, ts DESC""").fetchall()
    for d, t, h in rows:
        print(f"  {d:16s} {t:5.2f} {'HARD' if h else ''}")

    print("\n== verdicts (pm25, count) ==")
    for d, v, n in c.execute("""SELECT device_id, verdict, count(*) FROM murm_internal.anomaly_score
                                WHERE normalized_code='pm25' GROUP BY 1,2 ORDER BY 1,2"""):
        print(f"  {d:16s} {v:16s} {n}")

    print("\n== late data ==")
    print("  ", c.execute("SELECT count(*), sum(n_late) FROM murm_internal.reanalysis_queue").fetchone())

    # forecast skill at the cells holding healthy picos
    step = s.grid_step_deg
    healthy = [d for d, (t, p, la, lo) in dev.items() if t == "pico" and d not in ("pico-001", "pico-002")]
    truth = defaultdict(dict)
    for d, h, v in c.execute("""SELECT device_id, date_trunc('hour', ts), avg(value) FROM readings
                                WHERE normalized_code='pm25' GROUP BY 1,2"""):
        truth[d][h] = v
    err, pers, cov = defaultdict(list), defaultdict(list), defaultdict(list)
    for d in healthy:
        cid = cell_id(dev[d][2], dev[d][3], step)
        for issued, valid, val, lo, hi in c.execute(
                """SELECT issued_at, valid_at, value, ci_low, ci_high FROM field_forecast
                   WHERE normalized_code='pm25' AND cell_id=%s AND extract(minute from issued_at)=0""", (cid,)):
            h = round((valid - issued.replace(minute=0, second=0, microsecond=0)).total_seconds() / 3600)
            y = truth[d].get(valid)
            y0 = truth[d].get(issued.replace(minute=0, second=0, microsecond=0) - __import__("datetime").timedelta(hours=1))
            if y is None or y0 is None:
                continue
            err[h].append(val - y); pers[h].append(y0 - y); cov[h].append(lo <= y <= hi)
    print("\n== forecast at healthy-pico cells (pm25, hourly issues) ==")
    print("   h   n   RMSE   persist  skill   cov90")
    for h in sorted(err):
        e, p = np.array(err[h]), np.array(pers[h])
        r, rp = np.sqrt(np.mean(e ** 2)), np.sqrt(np.mean(p ** 2))
        print(f"  {h:2d} {len(e):4d} {r:6.2f} {rp:8.2f} {1 - r / rp:+6.1%} {np.mean(cov[h]):6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
