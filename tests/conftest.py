"""Shared fixtures. The integration tests drive a real browser against a real
instance of the mock app, because the things worth testing here -- locator
ladders, frame traversal, injected faults -- do not exist in a unit test."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://localhost:8000"


def _up() -> bool:
    try:
        urllib.request.urlopen(f"{BASE_URL}/login", timeout=1)
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def mock_app():
    """A running mock app. Reuses one already listening on 8000."""
    if _up():
        yield BASE_URL
        return
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "mock_app.main:app", "--port", "8000",
         "--log-level", "warning"],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "MOCK_TENANT": "heritage"},
    )
    for _ in range(60):
        if _up():
            break
        time.sleep(0.5)
    else:
        process.terminate()
        pytest.fail("mock app did not start")
    yield BASE_URL
    process.terminate()
    process.wait(timeout=10)


def inject(mode: str) -> None:
    """Arm a fault. An endpoint, not a code edit."""
    request = urllib.request.Request(
        f"{BASE_URL}/admin/inject", data=json.dumps({"mode": mode}).encode(),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=5).read()


@pytest.fixture
def clean_faults(mock_app):
    inject("clear")
    yield
    inject("clear")


@pytest.fixture
def surface(mock_app):
    from src.surface.playwright_driver import PlaywrightSurface
    driver = PlaywrightSurface(headless=True)
    yield driver
    driver.close()


@pytest.fixture
def artifact():
    from src.artifact import store
    loaded = store.load(ROOT / "artifacts" / "member.open_subaccount.v1.json")
    loaded.status = "approved"
    return loaded


@pytest.fixture
def policy():
    from src.guardrails.allowlist import Policy
    return Policy.load(ROOT / "allowlist.yaml")


@pytest.fixture(autouse=True)
def _reset_redaction():
    from src.guardrails.redaction import clear_sensitive
    clear_sensitive()
    yield
    clear_sensitive()
