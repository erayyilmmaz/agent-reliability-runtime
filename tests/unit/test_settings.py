import pytest
from pydantic import ValidationError

from agent_runtime.settings import Settings


def test_settings_accept_valid_configuration() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://runtime:runtime@db:5432/runtime",
        redis_url="redis://cache:6379/1",
        rabbitmq_url="amqp://broker:5672/",
        worker_concurrency=8,
        provider_timeout_seconds=45,
    )

    assert settings.worker_concurrency == 8
    assert settings.provider_timeout_seconds == 45


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "mysql://db/runtime"),
        ("redis_url", "http://cache"),
        ("rabbitmq_url", "https://broker"),
        ("worker_concurrency", 0),
    ],
)
def test_settings_reject_invalid_configuration(field: str, value: str | int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_api_key_mode_requires_token() -> None:
    with pytest.raises(ValidationError, match="auth_token"):
        Settings(auth_mode="api_key")
