"""End-to-end loop against a live Postgres + Redis (see README "Local stack").

Uses its own database (plumage_test) and Redis db 15, so it never touches dev data.
"""
import os
from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest

from murmuration.artifacts import ArtifactStore
from murmuration.cli import cmd_train
from murmuration.config import Settings
from murmuration.cycle import Murmuration
from murmuration.plumage import Plumage
from murmuration.queue import RedisStreamQueue
from murmuration.state import RedisState
from murmuration.synthetic import make_fleet, seed_plumage, store_records

pytestmark = pytest.mark.integration

ADMIN = os.environ.get("MURM_TEST_PG_ADMIN", "postgresql://plumage@localhost:5432/postgres")
DSN = os.environ.get("MURM_TEST_PG_DSN", "postgresql://plumage@localhost:5432/plumage_test")
REDIS = os.environ.get("MURM_TEST_REDIS", "redis://localhost:6379/15")
CYCLES, DARK_FROM, DARK_TO = 120, 24, 60          # dark device offline for 3 h, then flushes


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    try:
        with psycopg.connect(ADMIN, autocommit=True) as c:
            c.execute("DROP DATABASE IF EXISTS plumage_test WITH (FORCE)")
            c.execute("CREATE DATABASE plumage_test")
        import redis
        redis.Redis.from_url(REDIS).flushdb()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"local stack not available: {e}")

    s = replace(Settings(), pg_dsn=DSN, redis_url=REDIS, stream_key="test:fanout",
                artifacts=f"file://{tmp_path_factory.mktemp('art')}")
    db = Plumage(DSN); db.migrate()
    fleet = make_fleet(n_pico=14, days_hist=21, days_live=1)
    live0 = 21 * 288
    seed_plumage(db, fleet, live0)
    version, report = cmd_train(s, ["pm25", "pm10"], days=21, until=fleet.time_of(live0))
    q, state = RedisStreamQueue(REDIS, s.stream_key), RedisState(REDIS)
    svc = Murmuration(s, db, q, state, ArtifactStore(s.artifacts))
    dark = fleet.devices[5].device_id
    buf, outs, trust_dark = [], [], {}
    for c in range(CYCLES):
        k = live0 + c
        now = fleet.time_of(k) + timedelta(seconds=30)
        recs = fleet.records_at(k)
        if DARK_FROM <= c < DARK_TO:
            buf += [r for r in recs if r.device_id == dark]
            recs = [r for r in recs if r.device_id != dark]
        elif c == DARK_TO:
            recs += buf
        store_records(db, recs, now)
        q.publish(recs)
        if c == DARK_TO:
            trust_dark["before"] = state.get_trust(dark, "pm25")
        out = svc.run_cycle(now)
        if c == DARK_TO:
            trust_dark["flush_cycle"] = out
            trust_dark["after"] = state.get_trust(dark, "pm25")
        outs.append(out)
    return dict(s=s, db=db, svc=svc, fleet=fleet, outs=outs, state=state, dark=dark,
                trust_dark=trust_dark, report=report, live0=live0, q=q)


def _trust(run, dev):
    return run["state"].get_trust(dev, "pm25")


def test_training_recalibrates_bands(run):
    rep = run["report"]["pm25"]
    assert rep["n_val"] > 100 and 0.85 <= rep["recal_cov90"] <= 0.95


def test_faults_lose_trust_healthy_keep_it(run):
    devs = run["fleet"].devices
    stuck, drift = _trust(run, devs[1].device_id), _trust(run, devs[2].device_id)
    assert stuck.hard_fault and stuck.trust == pytest.approx(run["s"].trust_floor)
    assert drift.trust < 0.3
    healthy = [_trust(run, d.device_id).trust for d in devs[3:14] if d.device_id != run["dark"]]
    assert sorted(healthy)[len(healthy) // 2] > 0.75          # median
    assert min(healthy) > 0.4                                  # no healthy device amputated


def test_late_flush_never_moves_trust(run):
    before = run["trust_dark"]["before"]
    out = run["trust_dark"]["flush_cycle"]
    assert out["late"] >= 2 * (DARK_TO - DARK_FROM - 3)      # buffered readings routed as late
    after = run["trust_dark"]["after"]
    # the flush cycle judges the dark device once, from its fresh readings only:
    # at most one EMA step — never one update per replayed (late) reading
    assert after.n_updates == before.n_updates + 1
    assert abs(after.trust - before.trust) <= run["s"].eta_loss
    n = run["db"].conn.execute("SELECT sum(n_late) FROM murm_internal.reanalysis_queue").fetchone()[0]
    assert n >= DARK_TO - DARK_FROM - 3


def test_personal_femto_excluded_from_ambient_field(run):
    v = run["db"].conn.execute(
        "SELECT DISTINCT verdict FROM murm_internal.anomaly_score WHERE device_id='femto-phone-01'").fetchall()
    assert v == [("unjudged",)]
    e = run["db"].conn.execute(
        "SELECT exposure_coef, class FROM murm_internal.device_exposure WHERE device_id='femto-phone-01' "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    assert e == (0.0, "personal")


def test_public_outputs_never_name_a_source(run):
    for table in ("field_forecast", "virtual_sensor"):
        cols = {r[0] for r in run["db"].conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='public' "
            "AND table_name=%s", (table,))}
        assert not cols & {"device_id", "instance_index", "contributors"}, table


def test_outputs_written_with_credibility(run):
    c = run["db"].conn
    n, mn, mx, prov = c.execute(
        "SELECT count(*), min(credibility), max(credibility), bool_or(provisional) FROM field_forecast").fetchone()
    assert n > 0 and 0 <= mn <= mx <= 1 and prov is False      # 21 days, 14 picos: past the floors
    assert c.execute("SELECT count(DISTINCT site_id) FROM virtual_sensor").fetchone()[0] == 3
    for t in ("device_trust", "device_exposure", "device_position", "anomaly_score"):
        assert c.execute(f"SELECT count(*) FROM murm_internal.{t}").fetchone()[0] > 0, t


def test_cycle_is_idempotent(run):
    c = run["db"].conn
    before = c.execute("SELECT count(*), sum(value) FROM field_forecast").fetchone()
    last = run["fleet"].time_of(run["live0"] + CYCLES - 1) + timedelta(seconds=30)
    # re-deliver the last batch and re-run the same cycle time: upserts, no duplicates
    run["q"].publish(run["fleet"].records_at(run["live0"] + CYCLES - 1))
    run["svc"].run_cycle(last)
    after = c.execute("SELECT count(*), sum(value) FROM field_forecast").fetchone()
    assert before[0] == after[0]


def test_young_deployment_is_provisional(tmp_path):
    """Under 14 days of history the score is capped and rows are marked provisional."""
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS plumage_young WITH (FORCE)")
        c.execute("CREATE DATABASE plumage_young")
    dsn = DSN.replace("plumage_test", "plumage_young")
    import redis
    redis.Redis.from_url(REDIS.replace("/15", "/14")).flushdb()
    s = replace(Settings(), pg_dsn=dsn, redis_url=REDIS.replace("/15", "/14"), stream_key="young",
                artifacts=f"file://{tmp_path}")
    db = Plumage(dsn); db.migrate()
    fleet = make_fleet(n_pico=8, days_hist=5, days_live=1, faults=False)
    live0 = 5 * 288
    seed_plumage(db, fleet, live0)
    q = RedisStreamQueue(s.redis_url, s.stream_key)
    svc = Murmuration(s, db, q, RedisState(s.redis_url), ArtifactStore(s.artifacts))
    recs = fleet.records_at(live0); store_records(db, recs, fleet.time_of(live0)); q.publish(recs)
    out = svc.run_cycle(fleet.time_of(live0) + timedelta(seconds=30))
    assert out["codes"]["pm25"]["provisional"] and out["codes"]["pm25"]["credibility"] <= 0.20
