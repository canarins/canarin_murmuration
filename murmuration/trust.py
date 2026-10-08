"""Reliability controller C (paper §4, eq. 2).

C does not act on the air; it acts on the system's trust in each
(device, channel). Trust is a slow, asymmetric EMA of agreement:

    w <- w + eta * (a - w),   eta = eta_loss if a < w else eta_gain,  eta_loss > eta_gain

Stabilizers against the runaway-trust loop (paper §7):
  * floor       — trust never reaches zero in the gray zone
  * hard gate   — unambiguous faults (flatline, sanity, nested) cut immediately
  * forced listening — a distrusted device is periodically assimilated at full
                       weight to test whether it is still wrong
  * duty cycle  — absence is not unreliability: no reading, no update
  * sparsity    — with too few neighbours the device cannot be judged: no update

Cold start uses partial pooling over the hierarchy type -> device_model -> instance.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass


@dataclass
class TrustState:
    trust: float
    hard_fault: bool = False
    n_updates: int = 0


@dataclass(frozen=True)
class TrustParams:
    eta_gain: float
    eta_loss: float
    floor: float
    probe_every_cycles: int


def agreement(residual: float, sigma: float) -> float:
    """Gaussian agreement in [0,1] between a reading and its neighbours-only twin."""
    if sigma <= 0 or not math.isfinite(residual):
        return 0.0
    z = residual / sigma
    return math.exp(-0.5 * z * z)


def update(state: TrustState, a: float, p: TrustParams) -> TrustState:
    w = state.trust
    eta = p.eta_loss if a < w else p.eta_gain
    w = w + eta * (a - w)
    return TrustState(trust=max(p.floor, min(1.0, w)), hard_fault=False, n_updates=state.n_updates + 1)


def hard_gate(state: TrustState, p: TrustParams) -> TrustState:
    return TrustState(trust=p.floor, hard_fault=True, n_updates=state.n_updates + 1)


def is_probe_cycle(device_id, code, cycle_index: int, p: TrustParams) -> bool:
    """Deterministic, staggered forced-listening schedule (one slot per device/day)."""
    slot = zlib.crc32(f"{device_id}|{code}".encode()) % p.probe_every_cycles
    return cycle_index % p.probe_every_cycles == slot


def assimilation_weight(state: TrustState, probing: bool) -> float:
    if state.hard_fault:
        return 0.0
    return 1.0 if probing else state.trust


def pooled_prior(instance_trusts: dict[str, list[float]], fleet_default: float,
                 shrink: float = 4.0) -> dict[str, float]:
    """Empirical-Bayes prior per device_model from its instances' current trust.

    instance_trusts: {device_model: [trust of each mature instance]}
    Returns {device_model: prior}, shrunk toward the fleet default with
    pseudo-count `shrink` (few instances -> stay near the fleet value).
    """
    out = {}
    for model, vals in instance_trusts.items():
        n = len(vals)
        m = sum(vals) / n if n else fleet_default
        out[model] = (n * m + shrink * fleet_default) / (n + shrink)
    return out
