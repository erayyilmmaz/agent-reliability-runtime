"""Static API-key verification without retaining or emitting the raw credential."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from agent_runtime.settings import Settings


@dataclass(frozen=True)
class AuthenticationResult:
    authenticated: bool
    credential_fingerprint: str | None
    failure_code: str | None


def authenticate_api_key(
    *, settings: Settings, x_api_key: str | None, authorization: str | None
) -> AuthenticationResult:
    """Authenticate X-API-Key or Bearer credentials against a configured SHA-256 hash."""

    if settings.auth_mode == "disabled":
        return AuthenticationResult(True, None, None)

    raw_key = _extract_raw_key(x_api_key=x_api_key, authorization=authorization)
    if raw_key is None:
        return AuthenticationResult(False, None, "AUTHENTICATION_REQUIRED")
    fingerprint = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
    configured_hash = settings.auth_api_key_hash
    if configured_hash is None:
        raise RuntimeError("API-key authentication has no configured credential hash")
    if not hmac.compare_digest(fingerprint, configured_hash.get_secret_value()):
        return AuthenticationResult(False, fingerprint, "INVALID_CREDENTIAL")
    return AuthenticationResult(True, fingerprint, None)


def _extract_raw_key(*, x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key is not None:
        return x_api_key.strip() or None
    if authorization is None:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()
