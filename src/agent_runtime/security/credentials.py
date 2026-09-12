"""Versioned, config-backed credentials. Registry changes require a process restart."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr

CLIENT_ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
KEY_PATTERN = r"arr_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})"
KDF_ITERATIONS = 600_000

HMAC_SCHEME = "hmac-sha256-v1"
PBKDF2_SCHEME = "pbkdf2-sha256-v1"
SUPPORTED_SCHEMES = (HMAC_SCHEME, PBKDF2_SCHEME)
DEFAULT_SCHEME = HMAC_SCHEME


class CredentialRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    key_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    principal_id: str = Field(pattern=f"^{CLIENT_ID_PATTERN}$", max_length=128)
    tenant_id: str = Field(pattern=f"^{CLIENT_ID_PATTERN}$", max_length=128)
    # Registries issued before PERF-001 carry pbkdf2-sha256-v1 and keep working;
    # they are simply slow to verify. New credentials are issued as HMAC.
    scheme: str = Field(default=DEFAULT_SCHEME, pattern=r"^(hmac-sha256-v1|pbkdf2-sha256-v1)$")
    salt: str = Field(pattern=r"^[0-9a-f]{32}$")
    verifier: SecretStr
    enabled: bool = True
    expires_at: AwareDatetime | None = None

    def is_active(self) -> bool:
        return self.enabled and (self.expires_at is None or self.expires_at > datetime.now(UTC))


def derive_verifier(raw_key: str, *, salt: str, pepper: str, scheme: str = DEFAULT_SCHEME) -> str:
    """Derive the stored verifier for a key under the record's own scheme.

    `hmac-sha256-v1` is the default because the secret being verified is 256
    bits from `secrets.token_urlsafe(32)`, not a human-chosen password. Key
    stretching exists to make offline *guessing* expensive; there is no guessing
    surface at 256 bits, so PBKDF2's 600k iterations bought no security here
    while costing ~61 ms of CPU on every authenticated request (PERF-001).

    The pepper still does the real work: an attacker holding only the registry
    cannot derive a verifier, and HMAC is not invertible, so holding both the
    registry and the pepper still reveals no key.
    """

    if scheme == HMAC_SCHEME:
        return hmac.new(
            pepper.encode(),
            b"arr-credential-hmac-v1\0" + bytes.fromhex(salt) + b"\0" + raw_key.encode(),
            "sha256",
        ).hexdigest()
    if scheme == PBKDF2_SCHEME:
        # Domain-separated HMAC keeps the deployment pepper separate from the registry.
        material = hmac.digest(pepper.encode(), b"arr-credential-v1\0" + raw_key.encode(), "sha256")
        return hashlib.pbkdf2_hmac("sha256", material, bytes.fromhex(salt), KDF_ITERATIONS).hex()
    raise ValueError(f"Unsupported credential scheme: {scheme}")


def issue_credential(
    *, principal_id: str, tenant_id: str, pepper: str, expires_at: datetime | None = None
) -> tuple[str, CredentialRecord]:
    """Generate a 256-bit random secret; the raw key must be delivered only to its owner."""
    if len(pepper) < 32:
        raise ValueError("Credential pepper must contain at least 32 characters")
    key_id = secrets.token_hex(16)
    raw_key = f"arr_{key_id}.{secrets.token_urlsafe(32)}"
    salt = secrets.token_hex(16)
    return raw_key, CredentialRecord(
        key_id=key_id,
        principal_id=principal_id,
        tenant_id=tenant_id,
        scheme=DEFAULT_SCHEME,
        salt=salt,
        verifier=SecretStr(
            derive_verifier(raw_key, salt=salt, pepper=pepper, scheme=DEFAULT_SCHEME)
        ),
        expires_at=expires_at,
    )


def registry_entry(record: CredentialRecord) -> dict[str, object]:
    """Explicit secret export for provisioning, never for logging or API responses."""
    entry = record.model_dump(mode="json")
    entry["verifier"] = record.verifier.get_secret_value()
    return entry
