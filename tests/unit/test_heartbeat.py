from __future__ import annotations

import json
import time
from pathlib import Path

from agent_runtime.observability.heartbeat import Heartbeat, read_heartbeat


def test_disabled_heartbeat_is_a_no_op(tmp_path: Path) -> None:
    """A process without a configured path must still run normally."""

    heartbeat = Heartbeat(None, component="dispatcher")
    assert heartbeat.enabled is False
    heartbeat.beat()
    assert list(tmp_path.iterdir()) == []


def test_beat_records_a_fresh_readable_status(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "heartbeat.json"
    Heartbeat(path, component="worker").beat()

    status = read_heartbeat(path)
    assert status is not None
    assert status.component == "worker"
    assert status.is_fresh(max_age_seconds=5)


def test_beat_leaves_no_partial_file_behind(tmp_path: Path) -> None:
    """The probe must never observe a half-written heartbeat."""

    path = tmp_path / "heartbeat.json"
    heartbeat = Heartbeat(path, component="scheduler")
    for _ in range(5):
        heartbeat.beat()

    assert [entry.name for entry in tmp_path.iterdir()] == ["heartbeat.json"]
    assert json.loads(path.read_text(encoding="utf-8"))["component"] == "scheduler"


def test_stale_heartbeat_is_not_fresh(tmp_path: Path) -> None:
    """A process that stopped making progress must fail its probe."""

    path = tmp_path / "heartbeat.json"
    path.write_text(
        json.dumps({"component": "worker", "timestamp": time.time() - 600}), encoding="utf-8"
    )

    status = read_heartbeat(path)
    assert status is not None
    assert status.is_fresh(max_age_seconds=60) is False


def test_missing_or_corrupt_heartbeat_reads_as_none(tmp_path: Path) -> None:
    assert read_heartbeat(tmp_path / "absent.json") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert read_heartbeat(corrupt) is None

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"component": "worker"}), encoding="utf-8")
    assert read_heartbeat(wrong_shape) is None

    boolean_timestamp = tmp_path / "bool.json"
    boolean_timestamp.write_text(
        json.dumps({"component": "worker", "timestamp": True}), encoding="utf-8"
    )
    assert read_heartbeat(boolean_timestamp) is None


def test_unwritable_path_does_not_raise_into_the_loop(tmp_path: Path) -> None:
    """A failed heartbeat must not stop the work it reports on."""

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    Heartbeat(blocker / "heartbeat.json", component="dispatcher").beat()
