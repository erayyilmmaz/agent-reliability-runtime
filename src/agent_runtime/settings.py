from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyUrl, Field, PrivateAttr, SecretStr, TypeAdapter, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_runtime.security.credentials import CredentialRecord


class Settings(BaseSettings):
    """Runtime configuration read from APP_ prefixed environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="APP_", case_sensitive=False, extra="ignore", hide_input_in_errors=True
    )

    database_url: AnyUrl = AnyUrl(
        "postgresql+asyncpg://runtime:runtime@localhost:5432/agent_runtime"
    )
    migration_database_url: AnyUrl | None = None
    redis_url: AnyUrl = AnyUrl("redis://localhost:6379/0")
    rabbitmq_url: AnyUrl = AnyUrl("amqp://runtime:runtime@localhost:5672/")
    allow_plaintext_transport: bool = False
    openai_api_key: SecretStr | None = None
    openai_base_url: AnyUrl = AnyUrl("https://api.openai.com/v1")
    openai_default_model: str = Field(default="gpt-5", min_length=1, max_length=128)
    environment: Literal["local", "test", "staging", "production"] = "production"
    auth_mode: Literal["disabled", "api_key"] = "api_key"
    auth_credentials: SecretStr | None = None
    auth_pepper: SecretStr | None = None
    _credentials: tuple[CredentialRecord, ...] = PrivateAttr(default=())
    rate_limit_requests: int = Field(default=60, ge=1, le=10_000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    max_request_bytes: int = Field(default=131_072, ge=1_024, le=1_048_576)
    request_body_timeout_seconds: int = Field(default=10, ge=1, le=60)
    regression_max_cases: int = Field(default=10, ge=1, le=10)
    regression_provider_call_budget: int = Field(default=20, ge=2, le=20)
    provider_max_output_tokens: int = Field(default=2048, ge=1, le=8192)
    principal_concurrent_runs: int = Field(default=20, ge=1, le=1000)
    tenant_concurrent_runs: int = Field(default=40, ge=1, le=2000)
    principal_running_runs: int = Field(default=1, ge=1, le=128)
    tenant_running_runs: int = Field(default=2, ge=1, le=256)
    principal_provider_calls: int = Field(default=120, ge=1, le=10000)
    tenant_provider_calls: int = Field(default=240, ge=1, le=20000)
    quota_window_seconds: int = Field(default=3600, ge=60, le=86400)
    audit_failure_policy: Literal["fail_closed", "fail_open"] = "fail_closed"
    audit_write_timeout_seconds: float = Field(default=2, ge=0.01, le=30, allow_inf_nan=False)
    trust_inbound_trace_context: bool = False
    deterministic_echo_input: bool = False
    payload_retention_days: int = Field(default=30, ge=1, le=3650)
    worker_concurrency: int = Field(default=4, ge=1, le=128)
    provider_timeout_seconds: int = Field(default=60, ge=1, le=600)
    retry_max_attempts: int = Field(default=3, ge=1, le=20)
    retry_base_delay_seconds: float = Field(default=1.0, ge=1, le=3600, allow_inf_nan=False)
    retry_max_backoff_seconds: float = Field(default=60.0, ge=1, le=86_400, allow_inf_nan=False)
    retry_attempt_timeout_seconds: float = Field(default=60.0, ge=1, le=600, allow_inf_nan=False)
    retry_scheduler_poll_interval_seconds: float = Field(default=1.0, gt=0, le=300)
    outbox_batch_size: int = Field(default=50, ge=1, le=500)
    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0, le=60)
    outbox_publish_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    execution_lease_seconds: int = Field(default=120, ge=5, le=3600)
    lease_recovery_poll_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    otel_enabled: bool = True
    otel_endpoint: AnyUrl = AnyUrl("http://localhost:4318")
    heartbeat_path: Path | None = None
    heartbeat_interval_seconds: float = Field(default=10.0, gt=0, le=300)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("database_url", "migration_database_url")
    @classmethod
    def database_must_be_postgres(cls, value: AnyUrl | None) -> AnyUrl | None:
        if value is not None and value.scheme not in {"postgresql", "postgresql+asyncpg"}:
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

    def model_post_init(self, __context: object) -> None:
        if self.retry_max_backoff_seconds < self.retry_base_delay_seconds:
            raise ValueError("retry_max_backoff_seconds must be at least retry_base_delay_seconds")
        if self.auth_mode == "disabled":
            if self.environment != "local":
                raise ValueError("disabled auth requires explicit environment=local")
            return
        if self.auth_credentials is None or self.auth_pepper is None:
            raise ValueError("auth_credentials and auth_pepper are required in api_key mode")
        if len(self.auth_pepper.get_secret_value()) < 32:
            raise ValueError("auth_pepper must contain at least 32 characters")
        try:
            credentials = TypeAdapter(tuple[CredentialRecord, ...]).validate_json(
                self.auth_credentials.get_secret_value()
            )
        except ValueError:
            raise ValueError("auth_credentials must be a valid credential registry") from None
        if not credentials or not any(record.is_active() for record in credentials):
            raise ValueError("auth_credentials must contain an active credential")
        identities: dict[str, str] = {}
        key_ids: set[str] = set()
        for record in credentials:
            if record.key_id in key_ids:
                raise ValueError("Duplicate credential key_id")
            key_ids.add(record.key_id)
            if re.fullmatch(r"[0-9a-f]{64}", record.verifier.get_secret_value()) is None:
                raise ValueError("Invalid credential verifier")
            if identities.setdefault(record.principal_id, record.tenant_id) != record.tenant_id:
                raise ValueError("A principal must be bound to exactly one tenant")
        self._credentials = credentials
        # Checked last: a missing credential registry is the more fundamental
        # misconfiguration and should be the error an operator sees first.
        self._enforce_transport_encryption()

    def _enforce_transport_encryption(self) -> None:
        """Require TLS for internal links outside development (SEC-TRAN-01).

        `allow_plaintext_transport` exists so that an in-cluster kind/k3d demo
        stays possible, but it must be set deliberately: a plaintext broker or
        cache link in a deployed environment is then a recorded decision rather
        than an unnoticed default.
        """

        if self.environment not in {"staging", "production"} or self.allow_plaintext_transport:
            return
        insecure = [
            name
            for name, url, secure_scheme in (
                ("redis_url", self.redis_url, "rediss"),
                ("rabbitmq_url", self.rabbitmq_url, "amqps"),
            )
            if url.scheme != secure_scheme
        ]
        if insecure:
            raise ValueError(
                f"{self.environment} requires TLS for {', '.join(insecure)}; "
                "use rediss:// and amqps://, or set allow_plaintext_transport"
            )

    @property
    def credentials(self) -> tuple[CredentialRecord, ...]:
        return self._credentials

    @property
    def effective_migration_database_url(self) -> AnyUrl:
        """The DDL credential, falling back to the runtime credential (SEC-018)."""

        return self.migration_database_url or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
