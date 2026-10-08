"""murmuration — command-line entry point.

  murmuration migrate [--outputs-only]     apply db/migrations (stub Plumage + outputs)
  murmuration seed [--days 21]             synthetic Paris fleet + history (local dev only)
  murmuration train [--code pm25 ...]      fit field params on Plumage history -> new artifact
  murmuration cycle                        run one nowcast cycle now
  murmuration run [--interval 300]         run cycles forever (the Fargate task)
  murmuration simulate [--cycles 24]       replay synthetic live data through the full loop
  murmuration rollback VERSION             point `current` at an older artifact
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from .artifacts import ArtifactStore
from .config import Settings
from .cycle import Murmuration
from .field import fit_params, residual_scales
from .geo import to_km
from .plumage import Plumage
from .queue import RedisStreamQueue, SQSQueue
from .state import RedisState


def _queue(s: Settings):
    if s.queue == "sqs":
        import os
        return SQSQueue(s.sqs_url, os.environ.get("MURM_SQS_ENDPOINT") or None)
    return RedisStreamQueue(s.redis_url, s.stream_key)


def _service(s: Settings) -> Murmuration:
    return Murmuration(s, Plumage(s.pg_dsn), _queue(s), RedisState(s.redis_url), ArtifactStore(s.artifacts))


def cmd_train(s: Settings, codes: list[str], days: int, until: datetime | None = None):
    from .devices import exposure_prior
    db = Plumage(s.pg_dsn)
    devs = db.devices()
    ids = sorted(d for d, i in devs.items() if exposure_prior(i)[0] >= 0.7)   # ambient stations
    judged = sorted(d for d, i in devs.items() if exposure_prior(i)[1] != "personal")
    # same projection centre as the live cycle (all enrolled devices)
    lat0 = float(np.mean([i.lat for i in devs.values()])); lon0 = float(np.mean([i.lon for i in devs.values()]))
    pos = to_km([devs[d].lat for d in ids], [devs[d].lon for d in ids], lat0, lon0)
    pos_j = to_km([devs[d].lat for d in judged], [devs[d].lon for d in judged], lat0, lon0)
    ambient_mask = np.array([d in ids for d in judged])
    until = until or datetime.now(timezone.utc)
    payload = {"trained_at": until.isoformat(), "codes": {}, "report": {}, "residual": {}}
    for code in codes:
        S, hours, _ = db.hourly_history(code, until - timedelta(days=days), until, ids)
        p, rep = fit_params(S, hours, pos, s.length_scale_km)
        payload["codes"][code] = p.to_dict()
        payload["report"][code] = rep
        Sj, hj, _ = db.hourly_history(code, until - timedelta(days=days), until, judged)
        bias, scale = residual_scales(Sj, hj, pos_j, ambient_mask, p, s.neighbour_radius_km)
        payload["residual"][code] = {d: [float(b), float(sc)] for d, b, sc in zip(judged, bias, scale)
                                     if np.isfinite(b) and np.isfinite(sc)}
    version = "kriging-" + until.strftime("%Y%m%dT%H%M%S")
    ArtifactStore(s.artifacts).save(version, payload)
    return version, payload["report"]


def cmd_simulate(s: Settings, cycles: int, days_hist: int, n_pico: int, outage: bool = True):
    """Full local loop on synthetic data with a simulated clock.

    Each cycle: 'Birdhouse' writes the 5-min batch to Plumage and fans it out,
    Murmuration consumes it. Optionally one device goes dark for 6 h and then
    flushes its buffer (late data) — its trust must not move.
    """
    from .synthetic import make_fleet, seed_plumage, store_records
    fleet = make_fleet(n_pico=n_pico, days_hist=days_hist, days_live=max(1, cycles // 288 + 1))
    db = Plumage(s.pg_dsn)
    live0 = days_hist * 24 * 60 // 5
    seed_plumage(db, fleet, live0)
    version, _ = cmd_train(s, ["pm25", "pm10"], days=days_hist, until=fleet.time_of(live0))
    q = _queue(s)
    svc = Murmuration(s, db, q, RedisState(s.redis_url), ArtifactStore(s.artifacts))
    dark = fleet.devices[5].device_id if outage else None
    buffered = []
    out = []
    for c in range(cycles):
        k = live0 + c
        now = fleet.time_of(k) + timedelta(seconds=30)
        recs = fleet.records_at(k)
        if dark and c < 72:                       # 6 h outage, buffered on device
            buffered += [r for r in recs if r.device_id == dark]
            recs = [r for r in recs if r.device_id != dark]
        elif dark and c == 72:                    # reconnects: flushes 6 h of old readings
            recs += buffered
        store_records(db, recs, now)
        q.publish(recs)
        out.append(svc.run_cycle(now))
    return version, out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="murmuration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate"); m.add_argument("--outputs-only", action="store_true")
    sd = sub.add_parser("seed"); sd.add_argument("--days", type=int, default=21)
    sd.add_argument("--picos", type=int, default=14)
    tr = sub.add_parser("train"); tr.add_argument("--code", nargs="+", default=["pm25", "pm10"])
    tr.add_argument("--days", type=int, default=30)
    sub.add_parser("cycle")
    rn = sub.add_parser("run"); rn.add_argument("--interval", type=int, default=300)
    sm = sub.add_parser("simulate"); sm.add_argument("--cycles", type=int, default=96)
    sm.add_argument("--days", type=int, default=21); sm.add_argument("--picos", type=int, default=14)
    rb = sub.add_parser("rollback"); rb.add_argument("version")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = Settings()

    if a.cmd == "migrate":
        Plumage(s.pg_dsn).migrate(include_stub=not a.outputs_only); print("migrated")
    elif a.cmd == "seed":
        from .synthetic import make_fleet, seed_plumage
        f = make_fleet(n_pico=a.picos, days_hist=a.days, days_live=1,
                       start=datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                       - timedelta(days=a.days))
        seed_plumage(Plumage(s.pg_dsn), f, a.days * 288); print(f"seeded {len(f.devices)} devices")
    elif a.cmd == "train":
        v, rep = cmd_train(s, a.code, a.days); print(v); print(json.dumps(rep, indent=1))
    elif a.cmd == "cycle":
        print(json.dumps(_service(s).run_cycle(), indent=1, default=str))
    elif a.cmd == "run":
        svc = _service(s)
        while True:
            t0 = time.time()
            try:
                logging.info("cycle %s", json.dumps(svc.run_cycle(), default=str))
            except Exception:
                logging.exception("cycle failed; next cycle retries (writes are idempotent)")
            time.sleep(max(1, a.interval - (time.time() - t0)))
    elif a.cmd == "simulate":
        v, out = cmd_simulate(s, a.cycles, a.days, a.picos)
        print(f"model {v}"); print(json.dumps(out[-1], indent=1, default=str))
    elif a.cmd == "rollback":
        ArtifactStore(s.artifacts).set_current(a.version); print(f"current -> {a.version}")


if __name__ == "__main__":
    main()
