"""The ``SMSTUNNEL_*`` settings, read from ``django.conf.settings`` and checked.

=================================  =========================================================================
``SMSTUNNEL_API_KEY``              Required. A key from your SMSTunnel dashboard (Apps → Add an app).
``SMSTUNNEL_DEVICE_ID``            The phone that sends, when a call doesn't name one (default: SMSTunnel
                                   picks one of your phones).
``SMSTUNNEL_DEFAULT_COUNTRY_CODE`` Completes numbers written without ``+`` or ``00``, e.g. ``"+40"``.
``SMSTUNNEL_BASE_URL``             ``"https://smstunnel.io"``; change it only for a test server.
``SMSTUNNEL_TIMEOUT``              Seconds for each request (default 30).
=================================  =========================================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "InvalidSetting",
    "MissingSetting",
    "SmsTunnelSettings",
    "api_key",
    "base_url",
    "default_country_code",
    "device_id",
    "get_settings",
    "timeout",
]

DEFAULT_BASE_URL = "https://smstunnel.io"
DEFAULT_TIMEOUT = 30.0

_BASE_URL = re.compile(r"https?://[^/\s?#]+(/[^\s?#]*)?", re.IGNORECASE)
_COUNTRY_CODE = re.compile(r"(\+|00)?[1-9]\d{0,3}")


class MissingSetting(ImproperlyConfigured):
    """A required ``SMSTUNNEL_*`` setting is not set."""


class InvalidSetting(ImproperlyConfigured):
    """An ``SMSTUNNEL_*`` setting has a value SMSTunnel cannot use."""


@dataclass(frozen=True, slots=True)
class SmsTunnelSettings:
    """The ``SMSTUNNEL_*`` settings, checked."""

    api_key: str
    base_url: str
    timeout: float
    device_id: str | None
    default_country_code: str | None

    def __repr__(self) -> str:
        # Never show the key, not even in a traceback.
        return (
            f"SmsTunnelSettings(base_url={self.base_url!r}, timeout={self.timeout!r}, "
            f"device_id={self.device_id!r}, default_country_code={self.default_country_code!r})"
        )


def get_settings() -> SmsTunnelSettings:
    """The ``SMSTUNNEL_*`` settings.

    :raises ImproperlyConfigured: when ``SMSTUNNEL_API_KEY`` is missing or a setting has the wrong type.
    """
    return SmsTunnelSettings(
        api_key=api_key(),
        base_url=base_url(),
        timeout=timeout(),
        device_id=device_id(),
        default_country_code=default_country_code(),
    )


def api_key() -> str:
    value = _setting("SMSTUNNEL_API_KEY")
    if value is None or (isinstance(value, str) and not value.strip()):
        raise MissingSetting(
            "SMSTUNNEL_API_KEY is not set. Create a key in your SMSTunnel dashboard (Apps → Add an app), keep it "
            'in an environment variable and read it in settings.py: SMSTUNNEL_API_KEY = os.environ["SMSTUNNEL_API_KEY"]'
        )
    if not isinstance(value, str):
        raise InvalidSetting(f"SMSTUNNEL_API_KEY must be a string (got {type(value).__name__}).")
    return value.strip()


def base_url() -> str:
    value = _setting("SMSTUNNEL_BASE_URL")
    if value is None:
        return DEFAULT_BASE_URL
    url = value.strip().rstrip("/") if isinstance(value, str) else ""
    if not _BASE_URL.fullmatch(url):
        raise InvalidSetting(f'SMSTUNNEL_BASE_URL must look like "{DEFAULT_BASE_URL}" (got {value!r}).')
    return url


def timeout() -> float:
    value = _setting("SMSTUNNEL_TIMEOUT")
    if value is None:
        return DEFAULT_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, int | float) or not value > 0:
        raise InvalidSetting(f"SMSTUNNEL_TIMEOUT must be a number of seconds above 0 (got {value!r}).")
    return float(value)


def device_id() -> str | None:
    value = _setting("SMSTUNNEL_DEVICE_ID")
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidSetting(
            f"SMSTUNNEL_DEVICE_ID must be a string: the phone's ID from Phones in your SMSTunnel dashboard "
            f"(got {type(value).__name__})."
        )
    return value.strip() or None


def default_country_code() -> str | None:
    value = _setting("SMSTUNNEL_DEFAULT_COUNTRY_CODE")
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        raise InvalidSetting(f'SMSTUNNEL_DEFAULT_COUNTRY_CODE must be a string such as "+40" (got {value!r}).')
    code = value.strip()
    if not code:
        return None
    if not _COUNTRY_CODE.fullmatch(code):
        raise InvalidSetting(
            f'SMSTUNNEL_DEFAULT_COUNTRY_CODE must be a calling code such as "+40", "40" or "0040" (got {value!r}).'
        )
    return code


def _setting(name: str) -> Any:
    return getattr(settings, name, None)
