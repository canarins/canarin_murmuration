from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from murmuration import credibility as cred
from murmuration.artifacts import ArtifactStore
from murmuration.devices import DeviceInfo, exposure_prior, position_confidence
from murmuration.field import Assimilated, FieldParams, forecast
from murmuration.gates import CodeSpec, attribute, flatline, nested_violations, sanity
from murmuration.routing import Route, Router
from murmuration.trust import (TrustParams, TrustState, agreement, assimilation_weight, hard_gate,
                               is_probe_cycle, pooled_prior, update)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TP = TrustParams(eta_gain=0.02, eta_loss=0.08, floor=0.05, probe_every_cycles=288)


# ---------------------------------------------------------------- routing --
@pytest.mark.parametrize("age_s,expected", [
    (0, Route.FRESH), (900, Route.FRESH), (901, Route.LATE), (48 * 3600, Route.LATE),
    (48 * 3600 + 1, Route.STALE), (-60, Route.FRESH), (-121, Route.SKEW)])
def test_router(age_s, expected):
    r = Router(900, 48 * 3600, 120)
    assert r.route(NOW - timedelta(seconds=age_s), NOW) is expected


# ------------------------------------------------------------------ trust --
def test_trust_is_easier_to_lose_than_regain():
    lose = update(TrustState(0.8), 0.0, TP).trust
    gain = update(TrustState(0.2), 1.0, TP).trust
    assert 0.8 - lose > gain - 0.2


def test_trust_floor_and_hard_gate():
    s = TrustState(0.9)
    for _ in range(2000):
        s = update(s, 0.0, TP)
    assert s.trust == pytest.approx(TP.floor)
    g = hard_gate(TrustState(0.9), TP)
    assert g.hard_fault and g.trust == TP.floor and assimilation_weight(g, probing=True) == 0.0


def test_healthy_device_converges_high():
    rng = np.random.default_rng(0)
    s = TrustState(0.5)
    for _ in range(3000):
        s = update(s, agreement(rng.normal(), 1.5), TP)
    # asymmetric EMA settles below mean agreement (~0.83 at k=1.5), well clear of fault levels
    assert 0.6 < s.trust < 0.9


def test_probe_schedule_once_per_period():
    hits = [c for c in range(288 * 3) if is_probe_cycle(1001, 2, c, TP)]
    assert len(hits) == 3 and hits[1] - hits[0] == 288


def test_pooled_prior_shrinks_small_groups():
    p = pooled_prior({"bad-rev": [0.2], "good-rev": [0.9] * 40}, fleet_default=0.6)
    assert 0.2 < p["bad-rev"] < 0.6 and p["good-rev"] > 0.85


# ------------------------------------------------------------------ gates --
PM25, PM10 = 2, 3
SPECS = {
    PM10: CodeSpec(PM10, "PM10", "target", True, 0.6, 0, 2000, None, None),
    PM25: CodeSpec(PM25, "PM2.5", "target", True, 0.6, 0, 2000, PM10, "le_parent"),
}


def test_sanity_window():
    assert sanity(10, SPECS[PM25]) and not sanity(-1, SPECS[PM25]) and not sanity(np.nan, SPECS[PM25])


def test_flatline_uses_trailing_run():
    s = np.r_[np.random.default_rng(1).normal(10, 1, 18), [7.0] * 6]
    assert flatline(s, 6) and not flatline(s[:-1], 6)


def test_nested_violation_and_attribution():
    assert nested_violations({PM25: 50, PM10: 20}, SPECS) == [(PM25, PM10)]
    assert nested_violations({PM25: 20, PM10: 20.5}, SPECS) == []
    assert attribute(4.0, 0.3, PM25, PM10) == PM25
    assert attribute(0.2, -5.0, PM25, PM10) == PM10


# ---------------------------------------------------------------- devices --
def test_exposure_priors_by_power_source():
    mk = lambda t, p: DeviceInfo(1, t, None, p, 0, 0, None, None)
    assert exposure_prior(mk("pico", "mains")) == (1.0, "outdoor")
    assert exposure_prior(mk("femto", "phone")) == (0.0, "personal")
    assert exposure_prior(mk("femto", "mains"))[1] == "indoor"
    assert position_confidence(15, 1.5) > position_confidence(150, 1.5)


# ------------------------------------------------------------------ field --
def _field(weights=None):
    pos = np.array([[0, 0], [1, 0], [0, 1], [1, 1], [3, 3]], float)
    anom = np.array([2.0, 2.0, 2.0, 2.0, -1.0])
    w = np.ones(5) if weights is None else weights
    return Assimilated(pos, anom, w, FieldParams(s2=4, tau2=0.1, length_km=1.5, phi=0.8,
                                                  clim_hour=[10.0] * 24))


def test_kriging_interpolates_and_is_uncertain_far_away():
    post = _field()
    m, v = post.predict(np.array([[0.5, 0.5], [20, 20]]))
    assert m[0] == pytest.approx(2.0, abs=0.3)
    assert v[0] < v[1] and v[1] == pytest.approx(4.0, rel=1e-3)


def test_low_trust_device_pulls_less():
    q = np.array([[3, 3]])
    full = _field().predict(q)[0][0]
    distrusted = _field(np.array([1, 1, 1, 1, 0.05])).predict(q)[0][0]
    assert full < distrusted            # the -1 reading at (3,3) matters less


def test_forecast_band_widens_with_horizon():
    fc = forecast(_field(), np.array([[0.5, 0.5]]), 8, (1, 3, 6))
    widths = [float(hi[0] - lo[0]) for _, (_, lo, hi) in sorted(fc.items())]
    assert widths == sorted(widths)


# ------------------------------------------------------------ credibility --
def test_credibility_floors_and_timeliness():
    young = cred.Support(history_days=7, effective_fleet=20, mean_trust=1, coverage=1)
    assert cred.provisional(young) and cred.score_forecast(young) <= 0.20
    mature = cred.Support(history_days=90, effective_fleet=12, mean_trust=1, coverage=1)
    assert cred.score_forecast(mature) > 0.85
    # 16 enrolled but rarely on time -> effective fleet below the floor
    late_fleet = cred.Support(history_days=90, effective_fleet=3.5, mean_trust=1, coverage=1)
    assert cred.provisional(late_fleet)
    # virtual sensors: an extrapolated point scores below an interpolated one
    far = cred.Support(90, 12, 1, 1, local_density=0)
    near = cred.Support(90, 12, 1, 1, local_density=4)
    assert cred.score_virtual(far) < cred.score_virtual(near)


# -------------------------------------------------------------- artifacts --
def test_artifacts_are_immutable_and_rollback(tmp_path):
    st = ArtifactStore(f"file://{tmp_path}")
    st.save("v1", {"codes": {}}); st.save("v2", {"codes": {"2": {}}})
    assert st.load_current()[0] == "v2"
    with pytest.raises(FileExistsError):
        st.save("v1", {})
    st.set_current("v1")
    assert st.load_current()[0] == "v1"


# ------------------------------------------------------------------ queue --
def test_record_parses_birdhouse_stream_entry():
    from murmuration.queue import Record
    r = Record.from_fields({"device_id": "72", "pollutant_id": "415", "pollutant_type": "2",
                            "instance_index": "1", "ts": "1700000000", "value": "12.5",
                            "received_at": "1700021600.25"})
    assert r.device_id == 72 and r.ptype == 2 and r.value == 12.5
    assert (r.received_at - r.ts).total_seconds() == pytest.approx(21600.25)   # flushed 6 h late
