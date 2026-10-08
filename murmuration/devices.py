"""Per-device slow state priors: exposure and position (paper §4, Table 4).

Device type and power source are *declared*, so they seed the priors instead of
having to be inferred from residuals over days. Device type conditions the
interpretation of a reading (its noise, location error, assimilation weight) —
it is never a feature of the field value itself.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class DeviceInfo:
    device_id: str
    device_type: str            # pico | femto | other
    device_model: str | None
    power_source: str | None    # mains | battery | phone
    lat: float
    lon: float
    position_source: str | None
    accuracy_m: float | None


def exposure_prior(d: DeviceInfo) -> tuple[float, str]:
    """(coef, class). The coefficient is the ambient-field assimilation weight.

    A phone-powered Femto measures its carrier's exposure: that is the product,
    not a defect, so it gets weight 0 in the ambient field (class 'personal').
    A mains Femto is plugged into a wall socket — almost certainly indoors.
    """
    if d.device_type == "pico":
        return 1.0, "outdoor"
    if d.device_type == "femto":
        if d.power_source == "phone":
            return 0.0, "personal"
        return 0.15, "indoor"
    return 0.5, "partial"


def exposure_class(coef: float, personal: bool = False) -> str:
    if personal:
        return "personal"
    if coef >= 0.7:
        return "outdoor"
    if coef >= 0.3:
        return "partial"
    return "indoor"


def update_exposure(prev: float, device_anom: np.ndarray, field_anom: np.ndarray,
                    rate: float = 0.05, min_points: int = 72) -> float:
    """Slowly pull exposure toward the device<->outdoor-field coupling.

    Coupling is the correlation between the device's hourly anomaly and the
    outdoor field's anomaly at its location (neighbours only). An indoor device
    is buffered and decoupled; an outdoor one tracks the field.
    """
    m = np.isfinite(device_anom) & np.isfinite(field_anom)
    if m.sum() < min_points:
        return prev
    a, b = device_anom[m], field_anom[m]
    if np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return prev
    coupling = max(0.0, float(np.corrcoef(a, b)[0, 1]))
    return float(np.clip(prev + rate * (coupling - prev), 0.0, 1.0))


def position_prior_m(d: DeviceInfo) -> tuple[float, str]:
    """(uncertainty in metres, method). Uncertainty-first: never invents a position."""
    if d.accuracy_m is not None and d.accuracy_m > 0:
        return float(d.accuracy_m), "declared"
    if d.device_type == "pico" or d.position_source == "gnss":
        return 15.0, "declared"       # survey-once averaging brings this down later
    if d.position_source in ("wifi", "phone"):
        return 150.0, "declared"      # heavy tail of gross errors: reject, don't average
    return 50.0, "declared"


def position_confidence(uncertainty_m: float, length_scale_km: float) -> float:
    """How sharply a reading may inform the field at its stated location."""
    r = uncertainty_m / (length_scale_km * 1000.0)
    return float(1.0 / (1.0 + (3.0 * r) ** 2))
