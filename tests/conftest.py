from __future__ import annotations

from collections.abc import Iterator

import pytest

from django_smstunnel import client as client_module

from .support import FakeTransport


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTransport]:
    """The project's client answers from a FakeTransport instead of the network."""
    fake = FakeTransport()
    monkeypatch.setattr(client_module, "_make_transport", lambda: fake)
    client_module.get_client.cache_clear()
    yield fake
    client_module.get_client.cache_clear()
