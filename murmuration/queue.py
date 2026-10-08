"""Consumer of the Birdhouse fan-out stream (canarins_birdhouse/fanout.py).

Murmuration is strictly downstream of ingestion: it never reads the raw UDP
stream and never sits on the hot path. Birdhouse XADDs one entry per canonical
reading to a Redis stream on the shared ElastiCache; we read it with our own
consumer group.

Entry fields (strings): device_id, pollutant_id, pollutant_type, instance_index,
ts (bucketed unix s), value (canonical), received_at (unix s, server clock).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol


@dataclass(frozen=True)
class Record:
    device_id: int
    instance_index: int
    ptype: int                 # WebFront.pollutant_types.id
    ts: datetime               # event time (device, bucketed)
    value: float
    received_at: datetime      # arrival time (Birdhouse clock)

    @classmethod
    def from_fields(cls, f: dict) -> "Record":
        ts = datetime.fromtimestamp(int(float(f["ts"])), tz=timezone.utc)
        ra = datetime.fromtimestamp(float(f.get("received_at", f["ts"])), tz=timezone.utc)
        return cls(int(f["device_id"]), int(f.get("instance_index", 1)), int(f["pollutant_type"]),
                   ts, float(f["value"]), ra)

    def to_fields(self) -> dict:
        return {"device_id": str(self.device_id), "pollutant_id": "0",
                "pollutant_type": str(self.ptype), "instance_index": str(self.instance_index),
                "ts": str(int(self.ts.timestamp())), "value": repr(self.value),
                "received_at": repr(self.received_at.timestamp())}


class Queue(Protocol):
    def drain(self, max_messages: int = 10_000) -> list[Record]: ...
    def publish(self, records: Iterable[Record]) -> int: ...


class MemoryQueue:
    def __init__(self):
        self.items: list[Record] = []

    def publish(self, records):
        records = list(records)
        self.items.extend(records)
        return len(records)

    def drain(self, max_messages=10_000):
        out, self.items = self.items[:max_messages], self.items[max_messages:]
        return out


class RedisStreamQueue:
    GROUP = "murmuration"

    def __init__(self, url: str, key: str, consumer: str = "worker"):
        import redis
        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=10)
        self.key, self.consumer = key, consumer
        try:
            self.r.xgroup_create(key, self.GROUP, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, records):
        """Used by the simulator and tests to stand in for Birdhouse."""
        n = 0
        pipe = self.r.pipeline()
        for rec in records:
            pipe.xadd(self.key, rec.to_fields(), maxlen=200_000, approximate=True)
            n += 1
        pipe.execute()
        return n

    def drain(self, max_messages=10_000):
        out, ids = [], []
        # first re-claim anything this consumer read but never acked (crashed mid-cycle)
        pending = self.r.xreadgroup(self.GROUP, self.consumer, {self.key: "0"}, count=max_messages)
        batches = list(pending or [])
        while sum(len(e) for _, e in batches) < max_messages:
            resp = self.r.xreadgroup(self.GROUP, self.consumer, {self.key: ">"},
                                     count=min(1000, max_messages - sum(len(e) for _, e in batches)))
            if not resp:
                break
            batches += resp
        for _, entries in batches:
            for mid, fields in entries:
                ids.append(mid)
                try:
                    out.append(Record.from_fields(fields))
                except Exception:
                    pass  # malformed entry: ack and drop (Birdhouse validated it upstream)
        if ids:
            self.r.xack(self.key, self.GROUP, *ids)
        return out
