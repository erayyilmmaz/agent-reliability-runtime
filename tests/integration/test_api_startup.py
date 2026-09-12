import os
import subprocess
import sys

from fastapi.testclient import TestClient

from agent_runtime.api.main import create_app
from agent_runtime.settings import Settings


def test_api_starts_without_worker_or_provider_initialization() -> None:
    app = create_app(Settings(environment="local", auth_mode="disabled"))

    with TestClient(app) as client:
        response = client.get("/healthz")
        assert client.get("/docs").status_code == 200

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


def test_unconfigured_api_process_refuses_startup() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from agent_runtime.processes import run_api; run_api()"],
        env={key: value for key, value in os.environ.items() if not key.startswith("APP_")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "auth_credentials and auth_pepper are required" in result.stderr
    assert "Uvicorn running" not in result.stderr
