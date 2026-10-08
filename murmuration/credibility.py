"""Credibility of model outputs (paper §6.11, §6.10).

Every row Murmuration writes carries a score in [0,1] and a band, so Plumage can
render a provisional estimate differently from a well-supported one and Chirping
can suppress alerts whose evidence is thin.

  forecast / per-device state : 0.7 * history + 0.3 * fleet
  virtual sensor              : 0.7 * (fleet x local density) + 0.3 * history
  both multiplied by mean trust x coverage of the contributing devices

Fleet size is counted by *timeliness* (sum of per-device on-time fractions),
not enrolment: a device that habitually flushes late never supports the live
field. Floors: < 14 days of history or < 4 effective devices -> capped at 0.20,
serve the baseline, label provisional.

The saturation constants are first-order fits to the paper's Table 16 curves;
recalibrate them once operational Plumage data is available.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Support:
    history_days: float
    effective_fleet: float      # sum of on-time fractions
    mean_trust: float
    coverage: float             # fraction of expected devices that reported this cycle
    local_density: int = 0      # devices within ~1 km of the query point (virtual sensors)


def _hist(days: float) -> float:
    return min(1.0, math.log1p(max(days, 0) / 7.0) / math.log1p(90.0 / 7.0))


def _fleet(n: float) -> float:
    return min(1.0, max(n, 0) / 16.0)


def _density(k: int) -> float:
    return min(1.0, 0.5 + 0.125 * k)     # an extrapolated point is never as good as an interpolated one


def band(score: float) -> str:
    if score < 0.35:
        return "low"
    if score < 0.6:
        return "medium"
    return "high"


def provisional(s: Support, min_days: int = 14, min_devices: int = 4) -> bool:
    return s.history_days < min_days or s.effective_fleet < min_devices


def score_forecast(s: Support, cap: float = 0.20, **floors) -> float:
    raw = (0.7 * _hist(s.history_days) + 0.3 * _fleet(s.effective_fleet)) * s.mean_trust * s.coverage
    raw = max(0.0, min(1.0, raw))
    return min(raw, cap) if provisional(s, **floors) else raw


def score_virtual(s: Support, cap: float = 0.20, **floors) -> float:
    raw = (0.7 * _fleet(s.effective_fleet) * _density(s.local_density) + 0.3 * _hist(s.history_days)) \
        * s.mean_trust * s.coverage
    raw = max(0.0, min(1.0, raw))
    return min(raw, cap) if provisional(s, **floors) else raw
