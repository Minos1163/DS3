import os
import pytest

from src.config.env_manager import EnvManager


@pytest.fixture(scope="session")
def api_key() -> str:
    k, s = EnvManager.get_api_credentials()
    return k or os.getenv("BINANCE_API_KEY")


@pytest.fixture(scope="session")
def api_secret() -> str:
    k, s = EnvManager.get_api_credentials()
    return s or os.getenv("BINANCE_SECRET")


@pytest.fixture(autouse=True)
def _set_pytest_flag(monkeypatch):
    """Set an env var so tests can detect they're running under pytest."""
    monkeypatch.setenv("PYTEST_RUNNING", "1")
    yield
