"""End-to-end loop against a live MySQL + Redis (see README "Local stack").

Uses its own schemas (the migrations create them) in a throw-away MySQL — run
scripts/reset_local_db.sh first or point MURM_TEST_* at a scratch server — and
Redis db 15 with its own key prefix, so it never touches dev state.
"""
import os
from dataclasses import replace
from datetime import timedelta

import pytest

from murmuration.artifacts import ArtifactStore
from murmuration.cli import PM10, PM25, cmd_train
from murmuration.config import Settings
from murmuration.cycle import Murmuration
from murmuration.plumage import connect_from_settings
from murmuration.queue import RedisStreamQueue
from murmuration.state import RedisState
from murmuration.synthetic import make_fleet, seed_plumage, store_records

pytestmark = pytest.mark.integration

CYCLES, DARK_FROM, DARK_TO = 120, 24, 60          # dark device offline for 3 h, then flushes


def _settings(tmp, stream, prefix, redis_db=15):
    return replace(Settings(), redis_db=redis_db, stream_key=stream, state_prefix=prefix,
                   artifacts=f"file://{tmp}")


def _reset(s):
    try:
        db = connect_from_settings(s)
        for sch in ("Murmuration", "MurmurationInternal", "Devices", "WebFront", "`Data`"):
            db.exec(f"DROP DATABASE IF EXISTS {sch}")
        db.migrate()
        import redis
        redis.Redis.from_url(s.redis_url()).flushdb()
        return db
    except Exception as e:  # pragma: no cover
        pytest.skip(f"local stack not available: {e}")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    s = _settings(tmp_path_factory.mktemp("art"), "test:fanout", "murmtest:")
    db = _reset(s)
    fleet = make_fleet(n_pico=14, days_hist=21, days_live=1)
    live0 = 21 * 288
    seed_plumage(db, fleet, live0)
    version, report = cmd_train(s, [PM25, PM10], days=21, until=fleet.time_of(live0))
    q, state = RedisStreamQueue(s.redis_url(), s.stream_key), RedisState(s.redis_url(), s.state_prefix)
    svc = Murmuration(s, db, q, state, ArtifactStore(s.artifacts))
    dark = fleet.devices[5].device_id
    buf, outs, trust_dark = [], [], {}
    for c in range(CYCLES):
        k = live0 + c
        now = fleet.time_of(k) + timedelta(seconds=30)
        recs = fleet.records_at(k, received_at=now)
        if DARK_FROM <= c < DARK_TO:
            buf += [r for r in recs if r.device_id == dark]
            recs = [r for r in recs if r.device_id != dark]
        elif c == DARK_TO:
            recs += [replace(r, received_at=now) for r in buf]      # old ts, new arrival
        store_records(db, recs)
        q.publish(recs)
        if c == DARK_TO:
            trust_dark["before"] = state.get_trust(dark, PM25)
        out = svc.run_cycle(now)
        if c == DARK_TO:
            trust_dark["flush_cycle"] = out
            trust_dark["after"] = state.get_trust(dark, PM25)
        outs.append(out)
    return dict(s=s, db=db, svc=svc, fleet=fleet, outs=outs, state=state, dark=dark,
                trust_dark=trust_dark, report=report, live0=live0, q=q)


def _trust(run, dev):
    return run["state"].get_trust(dev, PM25)


def test_training_recalibrates_bands(run):
    rep = run["report"][PM25]
    assert rep["n_val"] > 100 and 0.85 <= rep["recal_cov90"] <= 0.95


def test_faults_lose_trust_healthy_keep_it(run):
    devs = run["fleet"].devices
    stuck, drift = _trust(run, devs[1].device_id), _trust(run, devs[2].device_id)
    assert stuck.hard_fault and stuck.trust == pytest.approx(run["s"].trust_floor)
    assert drift.trust < 0.3
    healthy = [_trust(run, d.device_id).trust for d in devs[3:14] if d.device_id != run["dark"]]
    assert sorted(healthy)[len(healthy) // 2] > 0.75
    assert min(healthy) > 0.4


def test_late_flush_never_moves_trust(run):
    before, after, out = run["trust_dark"]["before"], run["trust_dark"]["after"], run["trust_dark"]["flush_cycle"]
    assert out["late"] >= 2 * (DARK_TO - DARK_FROM - 3)
    assert after.n_updates == before.n_updates + 1                 # judged once, on fresh data only
    assert abs(after.trust - before.trust) <= run["s"].eta_loss
    n = run["db"].exec("SELECT SUM(n_late) FROM MurmurationInternal.reanalysis_queue")[0][0]
    assert n >= DARK_TO - DARK_FROM - 3


def test_personal_femto_excluded_from_ambient_field(run):
    v = run["db"].exec("SELECT DISTINCT verdict FROM MurmurationInternal.anomaly_score WHERE device_id=2002")
    assert v == (("unjudged",),)
    e = run["db"].exec("SELECT exposure_coef, class FROM MurmurationInternal.device_exposure "
                       "WHERE device_id=2002 ORDER BY ts DESC LIMIT 1")[0]
    assert e == (0.0, "personal")


def test_public_outputs_never_name_a_source(run):
    for table in ("field_forecast", "virtual_sensor"):
        cols = {r[0] for r in run["db"].exec(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='Murmuration' "
            "AND table_name=%s", (table,))}
        assert cols and not cols & {"device_id", "instance_index", "contributors", "pollutant_id"}, table


def test_outputs_written_with_credibility(run):
    db = run["db"]
    n, mn, mx, prov = db.exec("SELECT COUNT(*), MIN(credibility), MAX(credibility), MAX(provisional) "
                              "FROM Murmuration.field_forecast")[0]
    assert n > 0 and 0 <= mn <= mx <= 1 and prov == 0
    assert db.exec("SELECT COUNT(DISTINCT site_id) FROM Murmuration.virtual_sensor")[0][0] == 3
    assert db.exec("SELECT DISTINCT code FROM Murmuration.field_forecast ORDER BY 1") == (("PM10",), ("PM2.5",))
    for t in ("device_trust", "device_exposure", "device_position", "anomaly_score"):
        assert db.exec(f"SELECT COUNT(*) FROM MurmurationInternal.{t}")[0][0] > 0, t


def test_cycle_is_idempotent(run):
    db = run["db"]
    before = db.exec("SELECT COUNT(*) FROM Murmuration.field_forecast")[0][0]
    last = run["fleet"].time_of(run["live0"] + CYCLES - 1) + timedelta(seconds=30)
    run["q"].publish(run["fleet"].records_at(run["live0"] + CYCLES - 1, received_at=last))
    run["svc"].run_cycle(last)
    assert db.exec("SELECT COUNT(*) FROM Murmuration.field_forecast")[0][0] == before


def test_unacked_entries_are_reclaimed_after_a_crash(run):
    """A consumer that read but never acked (crashed mid-cycle) sees those entries again."""
    import redis
    s = run["s"]
    r = redis.Redis.from_url(s.redis_url(), decode_responses=True)
    recs = run["fleet"].records_at(run["live0"] + 1)
    run["q"].publish(recs)
    r.xreadgroup(RedisStreamQueue.GROUP, "worker", {s.stream_key: ">"}, count=1000)   # read, no ack
    got = run["q"].drain()
    assert len(got) >= len(recs)


def test_young_deployment_is_provisional(tmp_path):
    """Under 14 days of history the score is capped and rows are marked provisional."""
    s = _settings(tmp_path, "young:fanout", "murmyoung:", redis_db=14)
    db = _reset(s)
    fleet = make_fleet(n_pico=8, days_hist=5, days_live=1, faults=False)
    live0 = 5 * 288
    seed_plumage(db, fleet, live0)
    q = RedisStreamQueue(s.redis_url(), s.stream_key)
    svc = Murmuration(s, db, q, RedisState(s.redis_url(), s.state_prefix), ArtifactStore(s.artifacts))
    now = fleet.time_of(live0) + timedelta(seconds=30)
    recs = fleet.records_at(live0, received_at=now); store_records(db, recs); q.publish(recs)
    out = svc.run_cycle(now)
    assert out["codes"]["PM2.5"]["provisional"] and out["codes"]["PM2.5"]["credibility"] <= 0.20
