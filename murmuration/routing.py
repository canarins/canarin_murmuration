"""Event-time routing (paper §5.3).

Event time is not arrival time. A device behind CGNAT that was unreachable
flushes buffered readings on reconnection; treating them as current would make
the trust controller punish the device for poor connectivity.

  fresh : within the current cycle window -> may update field state AND trust
  late  : older, inside the reanalysis horizon -> stored, training only, marks
          the hours for bounded reanalysis; NEVER moves a live weight
  stale : older still -> stored, nothing else
  skew  : timestamp in the future beyond tolerance -> stored, flagged, never assimilated
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Route(str, Enum):
    FRESH = "fresh"
    LATE = "late"
    STALE = "stale"
    SKEW = "skew"


@dataclass(frozen=True)
class Router:
    fresh_window_s: int
    reanalysis_horizon_s: int
    skew_tolerance_s: int

    def route(self, ts: datetime, now: datetime) -> Route:
        age = (now - ts).total_seconds()
        if age < -self.skew_tolerance_s:
            return Route.SKEW
        if age <= self.fresh_window_s:
            return Route.FRESH
        if age <= self.reanalysis_horizon_s:
            return Route.LATE
        return Route.STALE
