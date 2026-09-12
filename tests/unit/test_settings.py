import pytest
from pydantic import ValidationError

from agent_runtime.settings import Settings


def test_settings_accept_valid_configuration() -> None:
    settings = Settings(
        environment="local",
        auth_mode="disabled",
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


def test_api_key_mode_requires_registry_and_pepper() -> None:
    with pytest.raises(ValidationError, match="auth_credentials"):
        Settings(auth_mode="api_key")


def test_legacy_hash_does_not_satisfy_auth_configuration() -> None:
    with pytest.raises(ValidationError, match="auth_credentials"):
        Settings(auth_mode="api_key", auth_api_key_hash="raw-secret")


def test_openai_settings_keep_key_optional_until_provider_is_selected() -> None:
    settings = Settings(environment="local", auth_mode="disabled", openai_default_model="gpt-5")

    assert settings.openai_api_key is None
    assert str(settings.openai_base_url) == "https://api.openai.com/v1"


def test_defaults_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENVIRONMENT", "APP_AUTH_MODE", "APP_AUTH_CREDENTIALS", "APP_AUTH_PEPPER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError, match="auth_credentials"):
        Settings()


@pytest.mark.parametrize("environment", ["test", "staging", "production"])
def test_disabled_auth_requires_explicit_local_environment(environment: str) -> None:
    with pytest.raises(ValidationError, match="environment=local"):
        Settings(environment=environment, auth_mode="disabled")
