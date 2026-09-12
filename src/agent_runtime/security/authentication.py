"""Credential verification and server-controlled identity resolution."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_runtime.security.credentials import KEY_PATTERN, derive_verifier

if TYPE_CHECKING:
    from agent_runtime.settings import Settings


@dataclass(frozen=True)
class AuthenticationResult:
    authenticated: bool
    credential_fingerprint: str | None
    failure_code: str | None
    principal_id: str | None = None
    tenant_id: str | None = None


def authenticate_api_key(
    *, settings: Settings, x_api_key: str | None, authorization: str | None
) -> AuthenticationResult:
    """Verify a generated key; no caller-supplied identity participates in authorization."""

    if settings.auth_mode == "disabled":
        return AuthenticationResult(True, None, None)

    raw_key = _extract_raw_key(x_api_key=x_api_key, authorization=authorization)
    if raw_key is None:
        return AuthenticationResult(False, None, "AUTHENTICATION_REQUIRED")
    match = re.fullmatch(KEY_PATTERN, raw_key)
    if match is None:
        return AuthenticationResult(False, None, "INVALID_CREDENTIAL")
    record = next((item for item in settings.credentials if item.key_id == match[1]), None)
    if record is None or not record.is_active():
        return AuthenticationResult(False, None, "INVALID_CREDENTIAL")
    pepper = settings.auth_pepper
    if pepper is None:
        raise RuntimeError("API-key authentication has no credential pepper")
    verifier = derive_verifier(
        raw_key, salt=record.salt, pepper=pepper.get_secret_value(), scheme=record.scheme
    )
    if not hmac.compare_digest(verifier, record.verifier.get_secret_value()):
        return AuthenticationResult(False, None, "INVALID_CREDENTIAL")
    # This identifies the registry record, and cannot be used as a credential verifier.
    fingerprint = hashlib.sha256(f"arr-audit-v1:{record.key_id}".encode()).hexdigest()[:16]
    return AuthenticationResult(True, fingerprint, None, record.principal_id, record.tenant_id)


def _extract_raw_key(*, x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key is not None:
        return x_api_key.strip() or None
    if authorization is None:
        return None
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential.strip():
        return None
    return credential.strip()
