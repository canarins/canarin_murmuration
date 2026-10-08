"""Runtime settings, read from MURM_* environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    pg_dsn: str = field(default_factory=lambda: _env(
        "MURM_PG_DSN", "postgresql://plumage:plumage@localhost:5432/plumage"))
    redis_url: str = field(default_factory=lambda: _env("MURM_REDIS_URL", "redis://localhost:6379/0"))
    queue: str = field(default_factory=lambda: _env("MURM_QUEUE", "redis"))      # redis | sqs
    sqs_url: str = field(default_factory=lambda: _env("MURM_SQS_URL", ""))
    stream_key: str = field(default_factory=lambda: _env("MURM_STREAM", "birdhouse:fanout"))
    artifacts: str = field(default_factory=lambda: _env("MURM_ARTIFACTS", "file://./artifacts"))
    model_version: str = field(default_factory=lambda: _env("MURM_MODEL_VERSION", "baseline-0.1.0"))

    # cycle & event-time routing (paper §5.3)
    cycle_seconds: int = 300                 # ~5 min nowcast cadence
    fresh_window_s: int = 900                # a reading this recent may update state + trust
    reanalysis_horizon_s: int = 48 * 3600    # late: stored + training, never live state
    skew_tolerance_s: int = 120              # future timestamps beyond this = clock skew

    # field model
    horizons_h: tuple[int, ...] = (1, 2, 3, 6)
    grid_step_deg: float = 0.005             # ~550 m output cells over Paris
    grid_margin_deg: float = 0.01
    length_scale_km: float = 1.5             # covariance length scale (paper §6.9)
    history_days: int = 30

    # trust controller (paper eq. 2 + stabilizers)
    eta_gain: float = 0.02                   # η+ (regain, slow)
    eta_loss: float = 0.08                   # η− (loss, faster) — η− > η+
    trust_floor: float = 0.05
    probe_every_cycles: int = 288            # forced listening ≈ once a day per device
    min_neighbours_for_trust: int = 2
    neighbour_radius_km: float = 4.5         # 3 × length scale

    # credibility floors (paper §6.11)
    min_history_days: int = 14
    min_devices: int = 4
    provisional_cap: float = 0.20
