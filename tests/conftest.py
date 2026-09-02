"""Shared fixtures. Every test that cares about time sets an explicit offset, so no
test depends on when it happens to run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from chase import clock  # noqa: E402  (after the path insert, deliberately)


@pytest.fixture
def set_offset(monkeypatch):
    """Set CHASE_CLOCK_OFFSET for the duration of one test."""

    def _set(value: str):
        monkeypatch.setenv(clock.ENV_VAR, value)
        return clock.now()

    return _set


@pytest.fixture(autouse=True)
def _zero_offset(monkeypatch):
    """Default every test to a zero offset unless it asks for another."""
    monkeypatch.setenv(clock.ENV_VAR, "0d")


@pytest.fixture
def config_dir() -> Path:
    return REPO_ROOT / "config"
