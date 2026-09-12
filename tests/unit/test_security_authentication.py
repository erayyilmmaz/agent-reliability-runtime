from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_runtime.security import credentials
from agent_runtime.security.authentication import authenticate_api_key
from agent_runtime.security.cli import main as credential_cli
from agent_runtime.security.credentials import KEY_PATTERN, issue_credential, registry_entry
from agent_runtime.settings import Settings

PEPPER = "test-only-pepper-" * 3
KEY, RECORD = issue_credential(principal_id="alice", tenant_id="tenant-a", pepper=PEPPER)
ROTATED_KEY, ROTATED = issue_credential(principal_id="alice", tenant_id="tenant-a", pepper=PEPPER)


def settings_for(*records: dict[str, object], pepper: str = PEPPER) -> Settings:
    return Settings(auth_mode="api_key", auth_credentials=json.dumps(records), auth_pepper=pepper)


def authenticate(settings: Settings, key: str | None):
    return authenticate_api_key(settings=settings, x_api_key=key, authorization=None)


def test_key_generation_verification_identity_and_safe_fingerprint() -> None:
    assert re.fullmatch(KEY_PATTERN, KEY)
    result = authenticate(settings_for(registry_entry(RECORD)), KEY)
    assert result.authenticated
    assert (result.principal_id, result.tenant_id) == ("alice", "tenant-a")
    assert len(result.credential_fingerprint) == 16
    assert KEY not in repr(result)
    assert RECORD.verifier.get_secret_value() not in repr(result)
    assert RECORD.verifier.get_secret_value() not in repr(RECORD)
    assert RECORD.verifier != ROTATED.verifier
    assert RECORD.salt != ROTATED.salt


def test_missing_wrong_unknown_and_bearer_keys() -> None:
    settings = settings_for(registry_entry(RECORD))
    assert authenticate(settings, None).failure_code == "AUTHENTICATION_REQUIRED"
    for key in ("weak-key", ROTATED_KEY, KEY[:-2] + "xx"):
        result = authenticate(settings, key)
        assert not result.authenticated
        assert result.credential_fingerprint is None
        assert result.tenant_id is None
    assert authenticate_api_key(
        settings=settings, x_api_key=None, authorization=f"Bearer {KEY}"
    ).authenticated
    assert not authenticate(
        settings_for(registry_entry(RECORD), pepper="different-pepper" * 3), KEY
    ).authenticated


def test_rotation_overlap_revocation_and_expiry() -> None:
    settings = settings_for(registry_entry(RECORD), registry_entry(ROTATED))
    assert (
        authenticate(settings, KEY).principal_id == authenticate(settings, ROTATED_KEY).principal_id
    )
    assert authenticate(settings, KEY).authenticated
    assert authenticate(settings, ROTATED_KEY).authenticated
    for changes in (
        {"enabled": False},
        {"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    ):
        rotated = settings_for({**registry_entry(RECORD), **changes}, registry_entry(ROTATED))
        assert not authenticate(rotated, KEY).authenticated
        assert authenticate(rotated, ROTATED_KEY).authenticated
    assert not authenticate(settings_for(registry_entry(ROTATED)), KEY).authenticated


@pytest.mark.parametrize(
    "change",
    [
        {"principal_id": "x" * 129},
        {"tenant_id": "bad value"},
        {"verifier": "raw-secret"},
        {"scheme": "sha256"},
        {"salt": "not-a-salt"},
        {"expires_at": "2026-01-01T00:00:00"},
    ],
)
def test_invalid_registry_rejected_without_exposing_secrets(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as error:
        settings_for({**registry_entry(RECORD), **change})
    assert RECORD.verifier.get_secret_value() not in str(error.value)
    assert "raw-secret" not in str(error.value)


def test_duplicate_keys_ambiguous_principals_empty_registry_and_weak_pepper_rejected() -> None:
    for records in (
        (),
        (registry_entry(RECORD), registry_entry(RECORD)),
        (registry_entry(RECORD), {**registry_entry(ROTATED), "tenant_id": "tenant-b"}),
        ({**registry_entry(RECORD), "enabled": False},),
    ):
        with pytest.raises(ValidationError):
            settings_for(*records)
    with pytest.raises(ValidationError):
        settings_for(registry_entry(RECORD), pepper="weak")


def test_expiry_is_enforced_without_restarting(monkeypatch: pytest.MonkeyPatch) -> None:
    future = datetime.now(UTC) + timedelta(minutes=5)
    settings = settings_for({**registry_entry(RECORD), "expires_at": future.isoformat()})
    assert authenticate(settings, KEY).authenticated

    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return future + timedelta(seconds=1)

    monkeypatch.setattr(credentials, "datetime", LaterDatetime)
    assert not authenticate(settings, KEY).authenticated


def test_provisioning_cli_round_trip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("APP_AUTH_PEPPER", PEPPER)
    monkeypatch.setattr(
        sys, "argv", ["credential", "--principal", "cli-user", "--tenant", "cli-tenant"]
    )
    assert credential_cli() == 0
    provisioned = json.loads(capsys.readouterr().out)
    settings = settings_for(provisioned["credential"])
    assert authenticate(settings, provisioned["api_key"]).tenant_id == "cli-tenant"
    monkeypatch.delenv("APP_AUTH_PEPPER")
    with pytest.raises(SystemExit) as error:
        credential_cli()
    assert error.value.code == 2
    assert capsys.readouterr().out == ""


# --- PERF-001: verification scheme -----------------------------------------


def _legacy_pbkdf2_record(principal: str, tenant: str) -> tuple[str, dict[str, object]]:
    """Build a registry entry exactly as it was issued before PERF-001."""
    raw_key, record = issue_credential(principal_id=principal, tenant_id=tenant, pepper=PEPPER)
    entry = registry_entry(record)
    entry["scheme"] = credentials.PBKDF2_SCHEME
    entry["verifier"] = credentials.derive_verifier(
        raw_key,
        salt=record.salt,
        pepper=PEPPER,
        scheme=credentials.PBKDF2_SCHEME,
    )
    return raw_key, entry


def test_new_credentials_are_issued_with_the_hmac_scheme() -> None:
    _, record = issue_credential(principal_id="bob", tenant_id="tenant-b", pepper=PEPPER)
    assert record.scheme == credentials.HMAC_SCHEME
    assert credentials.DEFAULT_SCHEME == credentials.HMAC_SCHEME


def test_pbkdf2_registries_issued_before_perf_001_still_authenticate() -> None:
    """A registry in the old scheme must keep working without re-issuing keys."""
    legacy_key, legacy_entry = _legacy_pbkdf2_record("carol", "tenant-c")

    result = authenticate(settings_for(legacy_entry), legacy_key)

    assert result.authenticated
    assert (result.principal_id, result.tenant_id) == ("carol", "tenant-c")


def test_both_schemes_coexist_in_one_registry() -> None:
    """Rotation happens per credential, so a registry is mixed while it runs."""
    legacy_key, legacy_entry = _legacy_pbkdf2_record("dave", "tenant-d")
    modern_key, modern = issue_credential(principal_id="erin", tenant_id="tenant-e", pepper=PEPPER)
    settings = settings_for(legacy_entry, registry_entry(modern))

    assert authenticate(settings, legacy_key).principal_id == "dave"
    assert authenticate(settings, modern_key).principal_id == "erin"


def test_a_key_never_verifies_under_the_wrong_scheme() -> None:
    """Domain separation: the two schemes must not accept each other's digests."""
    raw_key, record = issue_credential(principal_id="frank", tenant_id="tenant-f", pepper=PEPPER)
    hmac_digest = record.verifier.get_secret_value()
    pbkdf2_digest = credentials.derive_verifier(
        raw_key, salt=record.salt, pepper=PEPPER, scheme=credentials.PBKDF2_SCHEME
    )

    assert hmac_digest != pbkdf2_digest

    mislabelled = registry_entry(record)
    mislabelled["scheme"] = credentials.PBKDF2_SCHEME
    assert not authenticate(settings_for(mislabelled), raw_key).authenticated


def test_verifier_still_depends_on_pepper_and_salt() -> None:
    """HMAC removes stretching, not the secret inputs the verifier is bound to."""
    raw_key, record = issue_credential(principal_id="gina", tenant_id="tenant-g", pepper=PEPPER)
    stored = record.verifier.get_secret_value()

    other_pepper = credentials.derive_verifier(raw_key, salt=record.salt, pepper="z" * 48)
    other_salt = credentials.derive_verifier(raw_key, salt="0" * 32, pepper=PEPPER)

    assert stored != other_pepper
    assert stored != other_salt
    wrong_pepper = settings_for(registry_entry(record), pepper="z" * 48)
    assert not authenticate(wrong_pepper, raw_key).authenticated


def test_unknown_scheme_is_rejected_rather_than_defaulted() -> None:
    with pytest.raises(ValueError, match="Unsupported credential scheme"):
        credentials.derive_verifier("arr_x.y", salt="0" * 32, pepper=PEPPER, scheme="md5-v1")

    entry = registry_entry(issue_credential(principal_id="h", tenant_id="t", pepper=PEPPER)[1])
    entry["scheme"] = "md5-v1"
    with pytest.raises(ValidationError):
        settings_for(entry)
