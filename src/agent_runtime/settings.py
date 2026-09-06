from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration read from APP_ prefixed environment variables."""

    model_config = SettingsConfigDict(env_prefix="APP_", case_sensitive=False, extra="ignore")

    database_url: AnyUrl = AnyUrl(
        "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime"
    )
    redis_url: AnyUrl = AnyUrl("redis://localhost:6379/0")
    rabbitmq_url: AnyUrl = AnyUrl("amqp://runtime:runtime@localhost:5672/")
    auth_mode: Literal["disabled", "api_key"] = "disabled"
    auth_token: SecretStr | None = None
    worker_concurrency: int = Field(default=4, ge=1, le=128)
    provider_timeout_seconds: int = Field(default=60, ge=1, le=600)
    retry_max_attempts: int = Field(default=3, ge=0, le=20)
    retry_base_delay_seconds: float = Field(default=1.0, gt=0, le=3600)
    outbox_batch_size: int = Field(default=50, ge=1, le=500)
    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    outbox_publish_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    execution_lease_seconds: int = Field(default=120, ge=5, le=3600)
    lease_recovery_poll_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    otel_endpoint: AnyUrl = AnyUrl("http://localhost:4318")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("database_url")
    @classmethod
    def database_must_be_postgres(cls, value: AnyUrl) -> AnyUrl:
        if value.scheme not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError("database_url must use postgresql or postgresql+asyncpg")
        return value

    @field_validator("redis_url")
    @classmethod
    def redis_must_use_redis_scheme(cls, value: AnyUrl) -> AnyUrl:
        if value.scheme not in {"redis", "rediss"}:
            raise ValueError("redis_url must use redis or rediss")
        return value

    @field_validator("rabbitmq_url")
    @classmethod
    def rabbitmq_must_use_amqp_scheme(cls, value: AnyUrl) -> AnyUrl:
        if value.scheme not in {"amqp", "amqps"}:
            raise ValueError("rabbitmq_url must use amqp or amqps")
        return value

    @field_validator("auth_token")
    @classmethod
    def api_key_mode_requires_token(cls, value: SecretStr | None, info: object) -> SecretStr | None:
        # Cross-field validation is completed in model_post_init below.
        return value

    def model_post_init(self, __context: object) -> None:
        if self.auth_mode == "api_key" and self.auth_token is None:
            raise ValueError("auth_token is required when auth_mode is api_key")


@lru_cache
def get_settings() -> Settings:
    return Settings()
