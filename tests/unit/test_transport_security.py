"""SEC-TRAN-01 and SEC-018: transport encryption and DDL credential separation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent_runtime.security.credentials import issue_credential, registry_entry
from agent_runtime.settings import Settings

PEPPER = "p" * 32
_, _RECORD = issue_credential(principal_id="p1", tenant_id="t1", pepper=PEPPER)
CREDENTIALS = json.dumps([registry_entry(_RECORD)])


def _settings(**overrides: object) -> Settings:
    # These tests are about the transport rule itself, so they override the
    # suite-wide opt-out from tests/conftest.py rather than inheriting it.
    base: dict[str, object] = {
        "environment": "production",
        "auth_mode": "api_key",
        "auth_credentials": CREDENTIALS,
        "auth_pepper": PEPPER,
        "allow_plaintext_transport": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    [
        ("redis_url", "redis://cache:6379/0"),
        ("rabbitmq_url", "amqp://runtime:runtime@broker:5672/"),
    ],
)
def test_production_rejects_plaintext_internal_transport(field: str, value: str) -> None:
    """A deployed environment must not fall back to an unencrypted internal link."""

    with pytest.raises(ValidationError) as error:
        _settings(**{field: value})
    assert field in str(error.value)


def test_production_accepts_tls_internal_transport() -> None:
    settings = _settings(
        redis_url="rediss://cache:6379/0",
        rabbitmq_url="amqps://runtime:runtime@broker:5671/",
    )
    assert settings.redis_url.scheme == "rediss"
    assert settings.rabbitmq_url.scheme == "amqps"


def test_plaintext_requires_an_explicit_opt_in() -> None:
    """Turning TLS off must be a recorded decision, not an unnoticed default."""

    settings = _settings(
        redis_url="redis://cache:6379/0",
        rabbitmq_url="amqp://runtime:runtime@broker:5672/",
        allow_plaintext_transport=True,
    )
    assert settings.allow_plaintext_transport is True


def test_local_development_is_not_constrained() -> None:
    settings = Settings(
        environment="local",
        auth_mode="disabled",
        redis_url="redis://localhost:6379/0",
        rabbitmq_url="amqp://runtime:runtime@localhost:5672/",
    )
    assert settings.redis_url.scheme == "redis"


def test_migration_url_defaults_to_the_runtime_credential() -> None:
    settings = _settings(
        redis_url="rediss://cache:6379/0",
        rabbitmq_url="amqps://runtime:runtime@broker:5671/",
    )
    assert settings.effective_migration_database_url == settings.database_url


def test_migration_url_overrides_the_runtime_credential() -> None:
    """SEC-018: the DDL role is separate from the role the API runs as."""

    settings = _settings(
        redis_url="rediss://cache:6379/0",
        rabbitmq_url="amqps://runtime:runtime@broker:5671/",
        database_url="postgresql+asyncpg://arr_runtime:x@db:5432/agent_runtime",
        migration_database_url="postgresql+asyncpg://arr_migrator:y@db:5432/agent_runtime",
    )
    assert "arr_migrator" in str(settings.effective_migration_database_url)
    assert "arr_runtime" in str(settings.database_url)


def test_migration_url_must_still_be_postgres() -> None:
    with pytest.raises(ValidationError):
        _settings(
            redis_url="rediss://cache:6379/0",
            rabbitmq_url="amqps://runtime:runtime@broker:5671/",
            migration_database_url="mysql://arr_migrator:y@db:3306/agent_runtime",
        )
