from __future__ import annotations

import pytest

from agent_runtime.infrastructure.messaging.worker import RabbitMqWorker


def test_worker_extracts_run_id_from_minimal_message() -> None:
    assert (
        str(
            RabbitMqWorker.run_id_from_message(
                b'{"event_id":"unused","run_id":"00000000-0000-0000-0000-000000000001"}'
            )
        )
        == "00000000-0000-0000-0000-000000000001"
    )


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"run_id":"invalid"}'])
def test_worker_rejects_malformed_message(body: bytes) -> None:
    with pytest.raises(ValueError):
        RabbitMqWorker.run_id_from_message(body)
