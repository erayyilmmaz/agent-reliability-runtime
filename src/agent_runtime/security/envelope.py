"""Opt-in encryption adapter boundary; not automatically enabled on existing data.

The local wrapper is for tests/development, not a production KMS. Deleting a
wrapped DEK does not shred recoverable copies in backups or process memory.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from agent_runtime.infrastructure.database.models import Run, SecurityAuditEvent, TenantKey
from agent_runtime.security.audit import safe_text


class KeyWrapper(Protocol):
    def wrap(self, dek: bytes) -> bytes: ...
    def unwrap(self, blob: bytes) -> bytes: ...


class LocalKeyWrapper:
    """Explicit development-only wrapping key; never derived from a password."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("A random 256-bit wrapping key is required")
        self._cipher = AESGCM(key)

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._cipher.encrypt(nonce, dek, b"arr-local-wrap-v1")

    def unwrap(self, blob: bytes) -> bytes:
        return self._cipher.decrypt(blob[:12], blob[12:], b"arr-local-wrap-v1")


class KeyDestroyedError(ValueError):
    pass


async def _lock_tenant(session: AsyncSession, tenant_id: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
        {"scope": f"t:{tenant_id}"},
    )


class TenantEnvelope:
    """All methods require a caller-owned transaction; no plaintext key cache."""

    def __init__(self, wrapper: KeyWrapper) -> None:
        self.wrapper = wrapper

    async def protect_policy(
        self, session: AsyncSession, *, tenant_id: str, run_id: UUID, policy: dict[str, Any]
    ) -> dict[str, Any]:
        """Prototype adapter: keep retry/routing controls readable; seal caller instructions."""
        if "_encrypted_instructions" in policy:
            raise ValueError("Policy is already protected")
        protected = dict(policy)
        if "instructions" in protected:
            protected["_encrypted_instructions"] = await self.encrypt(
                session,
                tenant_id=tenant_id,
                run_id=run_id,
                field="instructions",
                value=protected.pop("instructions"),
            )
        return protected

    async def _key(self, session: AsyncSession, tenant_id: str, *, create: bool) -> bytes:
        await _lock_tenant(session, tenant_id)
        row = await session.get(TenantKey, tenant_id, with_for_update=True)
        if row is None:
            if not create:
                raise KeyDestroyedError("Tenant key is unavailable")
            # Binding inside the wrapped plaintext prevents cross-tenant wrapped-key swaps.
            dek = os.urandom(32)
            row = TenantKey(
                tenant_id=tenant_id,
                last_encrypted_at=datetime.now(UTC),
                wrapped_dek=self.wrapper.wrap(tenant_id.encode() + b"\x00" + dek),
            )
            session.add(row)
            await session.flush()
        if row.destroyed_at is not None or row.wrapped_dek is None:
            raise KeyDestroyedError("Tenant key has been destroyed")
        raw = self.wrapper.unwrap(row.wrapped_dek)
        prefix = tenant_id.encode() + b"\x00"
        if not raw.startswith(prefix) or len(raw) != len(prefix) + 32:
            raise KeyDestroyedError("Tenant key binding is invalid")
        if create:
            row.last_encrypted_at = datetime.now(UTC)
        return raw[len(prefix) :]

    @staticmethod
    def _aad(tenant_id: str, run_id: UUID, field: str) -> bytes:
        if field not in {"input", "result", "instructions", "evaluation_result"}:
            raise ValueError("Unsupported encrypted field")
        return json.dumps(["arr-envelope-v1", tenant_id, str(run_id), field]).encode()

    async def encrypt(
        self, session: AsyncSession, *, tenant_id: str, run_id: UUID, field: str, value: Any
    ) -> dict[str, str]:
        key = await self._key(session, tenant_id, create=True)
        nonce = os.urandom(12)
        plaintext = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, self._aad(tenant_id, run_id, field))
        return {"format": "arr-envelope-v1", "data": base64.b64encode(nonce + ciphertext).decode()}

    async def decrypt(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        run_id: UUID,
        field: str,
        envelope: dict[str, str],
    ) -> Any:
        if envelope.get("format") != "arr-envelope-v1":
            raise ValueError("Unsupported envelope")
        key = await self._key(session, tenant_id, create=False)
        data = base64.b64decode(envelope["data"], validate=True)
        return json.loads(
            AESGCM(key).decrypt(data[:12], data[12:], self._aad(tenant_id, run_id, field))
        )


async def retention_candidates(
    session: AsyncSession, *, cutoff: datetime, limit: int = 100
) -> list[str]:
    """Whole-tenant erasure only: a fresh or active run makes the tenant ineligible."""
    unsafe_run = exists(
        select(Run.id).where(
            Run.client_id == TenantKey.tenant_id,
            (Run.completed_at.is_(None)) | (Run.completed_at > cutoff),
        )
    )
    return list(
        await session.scalars(
            select(TenantKey.tenant_id)
            .where(
                TenantKey.destroyed_at.is_(None),
                TenantKey.last_encrypted_at <= cutoff,
                ~unsafe_run,
            )
            .order_by(TenantKey.tenant_id)
            .limit(max(1, min(limit, 100)))
        )
    )


async def erase_tenant_key(
    session: AsyncSession,
    *,
    tenant_id: str,
    principal_id: str,
    dry_run: bool = True,
    confirm_tenant: str | None = None,
    cutoff: datetime | None = None,
) -> bool:
    """Destroy only the live wrapped key. No row deletion or automatic scheduled purge."""
    await _lock_tenant(session, tenant_id)
    row = await session.get(TenantKey, tenant_id, with_for_update=True)
    if row is None or row.destroyed_at is not None:
        return False
    unsafe: ColumnElement[bool] = Run.completed_at.is_(None)
    if cutoff is not None:
        if row.last_encrypted_at > cutoff:
            return False
        unsafe = unsafe | (Run.completed_at > cutoff)
    if await session.scalar(select(exists().where(Run.client_id == tenant_id, unsafe))):
        return False
    if dry_run:
        return True
    if confirm_tenant != tenant_id:
        raise ValueError("Explicit tenant confirmation is required")
    row.wrapped_dek = None
    row.destroyed_at = datetime.now(UTC)
    session.add(
        SecurityAuditEvent(
            event_type="TENANT_KEY_ERASED",
            outcome="ALLOWED",
            reason="EXPLICIT_ERASURE",
            client_id=safe_text(tenant_id, 128),
            principal_id=safe_text(principal_id, 128),
        )
    )
    return True
