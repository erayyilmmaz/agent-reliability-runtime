from fastapi.testclient import TestClient

from agent_runtime.api.main import create_app
from agent_runtime.settings import Settings


def test_api_starts_without_worker_or_provider_initialization() -> None:
    app = create_app(Settings())

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}
