"""Liveness heartbeats for background processes that expose no HTTP surface.

`kill -0 1` only proves PID 1 exists. A process whose loop has deadlocked, or
whose broker connection has silently dropped, still satisfies it. These
heartbeats are written only after real progress, so a probe that reads them is
asserting functionality rather than existence.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HEARTBEAT_SCHEMA_VERSION = "arr-heartbeat.v1"


@dataclass(frozen=True)
class HeartbeatStatus:
    """A heartbeat file that was read back successfully."""

    component: str
    age_seconds: float

    def is_fresh(self, max_age_seconds: float) -> bool:
        return 0 <= self.age_seconds <= max_age_seconds


class Heartbeat:
    """Records loop progress for a process that has no readiness endpoint."""

    def __init__(self, path: Path | None, *, component: str) -> None:
        self._path = path
        self._component = component

    @property
    def enabled(self) -> bool:
        return self._path is not None

    def beat(self) -> None:
        """Publish a fresh timestamp. Never raises into the caller's loop."""

        if self._path is None:
            return
        payload = json.dumps(
            {
                "schema_version": HEARTBEAT_SCHEMA_VERSION,
                "component": self._component,
                "timestamp": time.time(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            self._write_atomically(payload)
        except OSError:
            # A failed heartbeat must not stop the work it is reporting on. The
            # probe observes the stale file and restarts the pod instead.
            return

    def _write_atomically(self, payload: str) -> None:
        if self._path is None:
            return
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=directory, delete=False, encoding="utf-8", prefix=".heartbeat-"
        )
        try:
            with handle:
                handle.write(payload)
            os.replace(handle.name, self._path)
        except OSError:
            Path(handle.name).unlink(missing_ok=True)
            raise


def read_heartbeat(path: Path) -> HeartbeatStatus | None:
    """Return the recorded status, or None when it is missing or unreadable."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    component = payload.get("component")
    timestamp = payload.get("timestamp")
    if not isinstance(component, str) or not isinstance(timestamp, (int, float)):
        return None
    if isinstance(timestamp, bool):
        return None
    return HeartbeatStatus(component=component, age_seconds=time.time() - float(timestamp))
