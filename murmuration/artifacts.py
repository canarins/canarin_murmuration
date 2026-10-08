"""Versioned, immutable model artifacts (S3 in production, local dir in dev).
A rollback is a pointer change: the `current` file names the active version.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse


class ArtifactStore:
    def __init__(self, uri: str):
        u = urlparse(uri)
        self.scheme = u.scheme or "file"
        if self.scheme == "file":
            self.root = (u.netloc + u.path) if u.netloc else u.path
            os.makedirs(self.root, exist_ok=True)
        elif self.scheme == "s3":
            import boto3
            self.s3 = boto3.client("s3", endpoint_url=os.environ.get("MURM_S3_ENDPOINT") or None)
            self.bucket, self.prefix = u.netloc, u.path.lstrip("/")
        else:
            raise ValueError(f"unsupported artifact store {uri!r}")

    def _put(self, name: str, data: bytes):
        if self.scheme == "file":
            with open(os.path.join(self.root, name), "wb") as fh:
                fh.write(data)
        else:
            self.s3.put_object(Bucket=self.bucket, Key=f"{self.prefix}{name}", Body=data)

    def _get(self, name: str) -> bytes | None:
        try:
            if self.scheme == "file":
                with open(os.path.join(self.root, name), "rb") as fh:
                    return fh.read()
            return self.s3.get_object(Bucket=self.bucket, Key=f"{self.prefix}{name}")["Body"].read()
        except Exception:
            return None

    def save(self, version: str, payload: dict, make_current: bool = True):
        name = f"model-{version}.json"
        if self._get(name) is not None:
            raise FileExistsError(f"artifact {name} exists; versions are immutable")
        self._put(name, json.dumps(payload, indent=1).encode())
        if make_current:
            self._put("current", version.encode())

    def load_current(self) -> tuple[str, dict] | None:
        cur = self._get("current")
        if cur is None:
            return None
        version = cur.decode().strip()
        raw = self._get(f"model-{version}.json")
        return (version, json.loads(raw)) if raw else None

    def set_current(self, version: str):
        if self._get(f"model-{version}.json") is None:
            raise FileNotFoundError(version)
        self._put("current", version.encode())
