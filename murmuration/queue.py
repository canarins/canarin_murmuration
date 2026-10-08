"""Consumers of the Birdhouse fan-out. Murmuration is strictly downstream of
ingestion: it never reads the raw device stream and never sits on the hot path.

Message body (JSON), in the normalized vocabulary:
  {"device_id", "instance_index", "normalized_code", "ts", "value", "received_at"?}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Protocol


@dataclass(frozen=True)
class Record:
    device_id: str
    instance_index: int
    code: str
    ts: datetime
    value: float

    @classmethod
    def from_json(cls, body: str | dict) -> "Record":
        j = json.loads(body) if isinstance(body, str) else body
        ts = datetime.fromisoformat(str(j["ts"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return cls(str(j["device_id"]), int(j.get("instance_index", 0)),
                   str(j["normalized_code"]), ts, float(j["value"]))

    def to_json(self) -> str:
        return json.dumps({"device_id": self.device_id, "instance_index": self.instance_index,
                           "normalized_code": self.code, "ts": self.ts.isoformat(),
                           "value": self.value})


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
    """Local stand-in for SQS (the paper's fan-out is SQS backed by a Redis stream)."""
    GROUP = "murmuration"

    def __init__(self, url: str, key: str):
        import redis
        self.r = redis.Redis.from_url(url, decode_responses=True)
        self.key = key
        try:
            self.r.xgroup_create(key, self.GROUP, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP — already exists
            if "BUSYGROUP" not in str(e):
                raise

    def publish(self, records):
        n = 0
        pipe = self.r.pipeline()
        for rec in records:
            pipe.xadd(self.key, {"body": rec.to_json()})
            n += 1
        pipe.execute()
        return n

    def drain(self, max_messages=10_000):
        out, ids = [], []
        while len(out) < max_messages:
            resp = self.r.xreadgroup(self.GROUP, "worker", {self.key: ">"},
                                     count=min(1000, max_messages - len(out)))
            if not resp:
                break
            for _, entries in resp:
                for mid, fields in entries:
                    ids.append(mid)
                    try:
                        out.append(Record.from_json(fields["body"]))
                    except Exception:
                        pass  # malformed message: ack and drop (Birdhouse validated it)
        if ids:
            self.r.xack(self.key, self.GROUP, *ids)
        return out


class SQSQueue:
    def __init__(self, url: str, endpoint_url: str | None = None):
        import boto3
        self.sqs = boto3.client("sqs", endpoint_url=endpoint_url)
        self.url = url

    def publish(self, records):
        records = list(records)
        for i in range(0, len(records), 10):
            entries = [{"Id": str(j), "MessageBody": r.to_json()}
                       for j, r in enumerate(records[i:i + 10])]
            self.sqs.send_message_batch(QueueUrl=self.url, Entries=entries)
        return len(records)

    def drain(self, max_messages=10_000):
        out = []
        while len(out) < max_messages:
            resp = self.sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=10,
                                            WaitTimeSeconds=0)
            msgs = resp.get("Messages", [])
            if not msgs:
                break
            for m in msgs:
                try:
                    out.append(Record.from_json(m["Body"]))
                except Exception:
                    pass
            self.sqs.delete_message_batch(QueueUrl=self.url, Entries=[
                {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(msgs)])
        return out
