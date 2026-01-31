import os
import pytest


@pytest.fixture
def api_key() -> str:
    return os.environ.get("API_KEY", "testkey")


@pytest.fixture
def api_secret() -> str:
    return os.environ.get("API_SECRET", "testsecret")


@pytest.fixture(autouse=True)
def _pytest_running_env():
    os.environ.setdefault("PYTEST_RUNNING", "1")
    yield
