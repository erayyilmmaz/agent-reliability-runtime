import pytest

from agent_runtime.settings import get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
