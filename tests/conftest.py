import pytest

from agent_runtime.settings import get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def allow_plaintext_transport_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let tests build production Settings without also configuring TLS URLs.

    SEC-TRAN-01 makes `rediss://` and `amqps://` mandatory in staging and
    production. Most tests construct production Settings to exercise the
    secure-by-default auth path, not the transport rules, and forcing every one
    of them to carry TLS URLs would obscure what they actually assert.

    The control itself is covered directly in tests/unit/test_transport_security.py,
    which overrides this default explicitly.
    """

    monkeypatch.setenv("APP_ALLOW_PLAINTEXT_TRANSPORT", "true")
