from __future__ import annotations

import hashlib

from agent_runtime.security.authentication import authenticate_api_key
from agent_runtime.settings import Settings


def _secured_settings() -> Settings:
    raw_key = "unit-test-api-key"
    return Settings(
        auth_mode="api_key",
        auth_api_key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
    )


def test_missing_api_key_is_not_authenticated() -> None:
    result = authenticate_api_key(settings=_secured_settings(), x_api_key=None, authorization=None)

    assert result.authenticated is False
    assert result.failure_code == "AUTHENTICATION_REQUIRED"
    assert result.credential_fingerprint is None


def test_x_api_key_is_hashed_and_compared_without_retaining_raw_value() -> None:
    result = authenticate_api_key(
        settings=_secured_settings(), x_api_key="unit-test-api-key", authorization=None
    )

    assert result.authenticated is True
    assert result.credential_fingerprint == hashlib.sha256(b"unit-test-api-key").hexdigest()
    assert result.credential_fingerprint != "unit-test-api-key"


def test_bearer_credential_is_supported_and_invalid_key_is_rejected() -> None:
    accepted = authenticate_api_key(
        settings=_secured_settings(), x_api_key=None, authorization="Bearer unit-test-api-key"
    )
    rejected = authenticate_api_key(
        settings=_secured_settings(), x_api_key="wrong-key", authorization=None
    )

    assert accepted.authenticated is True
    assert rejected.authenticated is False
    assert rejected.failure_code == "INVALID_CREDENTIAL"
