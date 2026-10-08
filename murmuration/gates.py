"""Device-local physical gates (paper §4, §6.12).

Gates run on the raw record, per device and timestamp, before any model sees
the data. They need no model, no neighbour and no history (except flatline,
which needs the device's own recent series). A violated nested-fraction
inequality proves one of two channels is wrong without saying which, so
attribution is done separately against a neighbours-only expectation.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CodeSpec:
    mtype: int          # WebFront.pollutant_types.id
    code: str           # e.g. 'PM2.5' (display only)
    role: str
    decoder_head: bool
    cold_start_trust: float
    sanity_min: float | None
    sanity_max: float | None
    parent_type: int | None
    constraint_kind: str | None


@dataclass(frozen=True)
class GateHit:
    device_id: int
    mtype: int
    gate: str          # sanity | flatline | nested
    detail: str


def sanity(value: float, spec: CodeSpec) -> bool:
    if not np.isfinite(value):
        return False
    if spec.sanity_min is not None and value < spec.sanity_min:
        return False
    if spec.sanity_max is not None and value > spec.sanity_max:
        return False
    return True


def flatline(series: np.ndarray, min_points: int = 6) -> bool:
    """A sensor whose most recent `min_points` values never change is dead.

    Checks the trailing run, not the whole window, so a sensor that sticks
    mid-window is caught within `min_points` steps of sticking.
    """
    s = np.asarray(series, float)
    s = s[np.isfinite(s)]
    if s.size < min_points:
        return False
    return float(np.std(s[-min_points:])) < 1e-9


def nested_violations(record: dict[int, float], registry: dict[int, CodeSpec],
                      rel_tol: float = 0.05, abs_tol: float = 1.0) -> list[tuple[int, int]]:
    """Return (child, parent) measure-type pairs where child > parent beyond tolerance.

    record: {measure_type: value} for one device at one timestamp.
    """
    out = []
    for mtype, v in record.items():
        spec = registry.get(mtype)
        if spec is None or spec.constraint_kind != "le_parent" or spec.parent_type not in record:
            continue
        p = record[spec.parent_type]
        if v > p * (1 + rel_tol) + abs_tol:
            out.append((mtype, spec.parent_type))
    return out


def attribute(child_z: float, parent_z: float, child: int, parent: int) -> int:
    """Blame the channel that broke its own (neighbours-only) expectation more."""
    return child if abs(child_z) >= abs(parent_z) else parent
