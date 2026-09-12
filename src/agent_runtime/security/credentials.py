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


class CredentialRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    key_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    principal_id: str = Field(pattern=f"^{CLIENT_ID_PATTERN}$", max_length=128)
    tenant_id: str = Field(pattern=f"^{CLIENT_ID_PATTERN}$", max_length=128)
    scheme: str = Field(default="pbkdf2-sha256-v1", pattern=r"^pbkdf2-sha256-v1$")
    salt: str = Field(pattern=r"^[0-9a-f]{32}$")
    verifier: SecretStr
    enabled: bool = True
    expires_at: AwareDatetime | None = None

    def is_active(self) -> bool:
        return self.enabled and (self.expires_at is None or self.expires_at > datetime.now(UTC))


def derive_verifier(raw_key: str, *, salt: str, pepper: str) -> str:
    # Domain-separated HMAC keeps the deployment pepper separate from the registry.
    material = hmac.digest(pepper.encode(), b"arr-credential-v1\0" + raw_key.encode(), "sha256")
    return hashlib.pbkdf2_hmac("sha256", material, bytes.fromhex(salt), KDF_ITERATIONS).hex()


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
        salt=salt,
        verifier=SecretStr(derive_verifier(raw_key, salt=salt, pepper=pepper)),
        expires_at=expires_at,
    )


def registry_entry(record: CredentialRecord) -> dict[str, object]:
    """Explicit secret export for provisioning, never for logging or API responses."""
    entry = record.model_dump(mode="json")
    entry["verifier"] = record.verifier.get_secret_value()
    return entry
