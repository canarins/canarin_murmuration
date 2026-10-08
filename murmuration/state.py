"""Slow per-device state store: Redis/ElastiCache in production, memory in tests.

Holds trust per (device, channel), exposure per device, on-time fraction per
device, and the cycle counter. Authoritative copies are checkpointed to Plumage
(murm_internal.device_trust / device_exposure) by the cycle.
"""
from __future__ import annotations

import json
from typing import Protocol

from .trust import TrustState


class StateStore(Protocol):
    def get_trust(self, device_id: str, code: str) -> TrustState | None: ...
    def set_trust(self, device_id: str, code: str, s: TrustState) -> None: ...
    def all_trust(self) -> dict[tuple[str, str], TrustState]: ...
    def get_exposure(self, device_id: str) -> float | None: ...
    def set_exposure(self, device_id: str, v: float) -> None: ...
    def get_ontime(self, device_id: str) -> float | None: ...
    def set_ontime(self, device_id: str, v: float) -> None: ...
    def next_cycle(self) -> int: ...


class MemoryState:
    def __init__(self):
        self.trust: dict[tuple[str, str], TrustState] = {}
        self.exposure: dict[str, float] = {}
        self.ontime: dict[str, float] = {}
        self.cycle = -1

    def get_trust(self, d, c): return self.trust.get((d, c))
    def set_trust(self, d, c, s): self.trust[(d, c)] = s
    def all_trust(self): return dict(self.trust)
    def get_exposure(self, d): return self.exposure.get(d)
    def set_exposure(self, d, v): self.exposure[d] = v
    def get_ontime(self, d): return self.ontime.get(d)
    def set_ontime(self, d, v): self.ontime[d] = v

    def next_cycle(self):
        self.cycle += 1
        return self.cycle


class RedisState:
    def __init__(self, url: str, prefix: str = "murm:"):
        import redis
        self.PFX = prefix
        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=10)

    def get_trust(self, d, c):
        raw = self.r.hget(self.PFX + "trust", f"{d}|{c}")
        if raw is None:
            return None
        j = json.loads(raw)
        return TrustState(trust=j["t"], hard_fault=j["h"], n_updates=j["n"])

    def set_trust(self, d, c, s):
        self.r.hset(self.PFX + "trust", f"{d}|{c}",
                    json.dumps({"t": s.trust, "h": s.hard_fault, "n": s.n_updates}))

    def all_trust(self):
        out = {}
        for k, raw in self.r.hgetall(self.PFX + "trust").items():
            d, c = k.rsplit("|", 1)
            j = json.loads(raw)
            out[(int(d), int(c))] = TrustState(trust=j["t"], hard_fault=j["h"], n_updates=j["n"])
        return out

    def _getf(self, h, d):
        v = self.r.hget(self.PFX + h, d)
        return None if v is None else float(v)


    def get_exposure(self, d): return self._getf("exposure", d)
    def set_exposure(self, d, v): self.r.hset(self.PFX + "exposure", d, v)
    def get_ontime(self, d): return self._getf("ontime", d)
    def set_ontime(self, d, v): self.r.hset(self.PFX + "ontime", d, v)
    def next_cycle(self): return int(self.r.incr(self.PFX + "cycle")) - 1
