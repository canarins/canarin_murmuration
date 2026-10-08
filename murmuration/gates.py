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
    code: str
    role: str
    decoder_head: bool
    cold_start_trust: float
    sanity_min: float | None
    sanity_max: float | None
    parent_code: str | None
    constraint_kind: str | None


@dataclass(frozen=True)
class GateHit:
    device_id: str
    code: str
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


def nested_violations(record: dict[str, float], registry: dict[str, CodeSpec],
                      rel_tol: float = 0.05, abs_tol: float = 1.0) -> list[tuple[str, str]]:
    """Return (child, parent) pairs where child > parent beyond tolerance.

    record: {normalized_code: value} for one device at one timestamp.
    """
    out = []
    for code, v in record.items():
        spec = registry.get(code)
        if spec is None or spec.constraint_kind != "le_parent" or spec.parent_code not in record:
            continue
        p = record[spec.parent_code]
        if v > p * (1 + rel_tol) + abs_tol:
            out.append((code, spec.parent_code))
    return out


def attribute(child_z: float, parent_z: float, child: str, parent: str) -> str:
    """Blame the channel that broke its own (neighbours-only) expectation more."""
    return child if abs(child_z) >= abs(parent_z) else parent
