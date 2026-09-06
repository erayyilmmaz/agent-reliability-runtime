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


def test_api_key_mode_requires_credential_hash() -> None:
    with pytest.raises(ValidationError, match="auth_api_key_hash"):
        Settings(auth_mode="api_key")


def test_api_key_mode_rejects_a_non_hash_configuration() -> None:
    with pytest.raises(ValidationError, match="SHA-256"):
        Settings(auth_mode="api_key", auth_api_key_hash="raw-secret")


def test_openai_settings_keep_key_optional_until_provider_is_selected() -> None:
    settings = Settings(openai_default_model="gpt-5")

    assert settings.openai_api_key is None
    assert str(settings.openai_base_url) == "https://api.openai.com/v1"
