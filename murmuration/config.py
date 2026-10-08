"""Runtime settings, read from environment variables.

Database and Redis variables follow the Birdhouse / Plumage conventions on ECS
(DB_HOST, DB_PORT, DB_USER, DB_PASS, REDIS_HOST, REDIS_PORT, REDIS_TLS,
REDIS_AUTH_TOKEN). Murmuration-specific ones are prefixed MURM_.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # Canarin DB (RDS MySQL 8.4)
    db_host: str = field(default_factory=lambda: _env("DB_HOST", "127.0.0.1"))
    db_port: int = field(default_factory=lambda: int(_env("DB_PORT", "3306")))
    db_user: str = field(default_factory=lambda: _env("DB_USER", "plumage"))
    db_pass: str = field(default_factory=lambda: _env("DB_PASS", "plumage"))
    db_ssl_ca: str = field(default_factory=lambda: _env("DB_SSL_CA", ""))   # /etc/ssl/certs/rds-ca-bundle.pem on ECS

    # ElastiCache Redis (TLS + AUTH in prod) — model state AND the Birdhouse fan-out stream
    redis_host: str = field(default_factory=lambda: _env("REDIS_HOST", "127.0.0.1"))
    redis_port: int = field(default_factory=lambda: int(_env("REDIS_PORT", "6379")))
    redis_tls: bool = field(default_factory=lambda: _env("REDIS_TLS", "false").lower() in ("true", "1", "yes"))
    redis_auth: str = field(default_factory=lambda: _env("REDIS_AUTH_TOKEN", ""))
    redis_db: int = field(default_factory=lambda: int(_env("REDIS_DB", "0")))
    stream_key: str = field(default_factory=lambda: _env("BIRDHOUSE_FANOUT_STREAM", "birdhouse:fanout"))
    state_prefix: str = field(default_factory=lambda: _env("MURM_STATE_PREFIX", "murm:"))

    artifacts: str = field(default_factory=lambda: _env("MURM_ARTIFACTS", "file://./artifacts"))
    model_version: str = field(default_factory=lambda: _env("MURM_MODEL_VERSION", "baseline-0.1.0"))

    # cycle & event-time routing (paper §5.3)
    cycle_seconds: int = 300
    fresh_window_s: int = 900
    reanalysis_horizon_s: int = 48 * 3600
    skew_tolerance_s: int = 120

    # field model
    horizons_h: tuple[int, ...] = (1, 2, 3, 6)
    grid_step_deg: float = 0.005
    grid_margin_deg: float = 0.01
    length_scale_km: float = 1.5
    history_days: int = 30

    # trust controller (paper eq. 2 + stabilizers)
    eta_gain: float = 0.02
    eta_loss: float = 0.08
    trust_floor: float = 0.05
    probe_every_cycles: int = 288
    min_neighbours_for_trust: int = 2
    neighbour_radius_km: float = 4.5

    # credibility floors (paper §6.11)
    min_history_days: int = 14
    min_devices: int = 4
    provisional_cap: float = 0.20

    def redis_url(self) -> str:
        scheme = "rediss" if self.redis_tls else "redis"
        auth = f":{self.redis_auth}@" if self.redis_auth else ""
        return f"{scheme}://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"
