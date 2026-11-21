"""These tests need the settings of tests/settings_signin.py; a plain `pytest` leaves them out."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from django.core.cache import cache

collect_ignore_glob = [] if os.environ.get("DJANGO_SETTINGS_MODULE") == "tests.settings_signin" else ["*"]


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    """The limits are counted in the cache: every test starts from zero."""
    cache.clear()
    yield
    cache.clear()
