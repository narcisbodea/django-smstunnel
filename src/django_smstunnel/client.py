"""The project's SMSTunnel client, and the functions that send through it."""

from __future__ import annotations

import dataclasses
import functools
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import django
from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from smstunnel import ApiError, Client, RateLimitError, SendResult, TransportError
from smstunnel.transport import HttpRequest, HttpResponse, Transport, UrllibTransport

from ._version import __version__
from .conf import get_settings

__all__ = [
    "asend_sign_in_code",
    "asend_sms",
    "get_client",
    "is_retryable",
    "retry_after",
    "send_sign_in_code",
    "send_sms",
]

IDEMPOTENCY_KEY_MAX_LENGTH = 255

# The key of the send running in this thread or task. The smstunnel client hands each request to its transport in
# the same context, so _ProjectTransport can add the header to exactly that request.
_idempotency_key: ContextVar[str | None] = ContextVar("django_smstunnel_idempotency_key", default=None)


class _ProjectTransport:
    """Hands each request on to the real transport with this package's headers: ``Idempotency-Key`` (when the
    send has one) and a ``User-Agent`` that names django-smstunnel."""

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self._product = f"django-smstunnel/{__version__} Django/{django.get_version()}"

    def send(self, request: HttpRequest) -> HttpResponse:
        headers = dict(request.headers)
        user_agent = headers.get("User-Agent")
        headers["User-Agent"] = f"{user_agent} {self._product}" if user_agent else self._product
        key = _idempotency_key.get()
        if key is not None and request.method == "POST":
            headers["Idempotency-Key"] = key
        return self._inner.send(dataclasses.replace(request, headers=headers))


def _make_transport() -> Transport:
    return UrllibTransport()


@functools.cache
def get_client() -> Client:
    """The project's :class:`smstunnel.Client`, made from the ``SMSTUNNEL_*`` settings.

    It is made once and shared (it is safe to use from several threads); changing a setting in a test, with
    ``override_settings``, makes a new one. Use it for what the helpers here don't cover, such as
    ``get_client().get_message_status(message_id)`` or ``get_client().get_devices()``.

    :raises ImproperlyConfigured: when ``SMSTUNNEL_API_KEY`` is missing or a setting has the wrong type.
    """
    conf = get_settings()
    try:
        return Client(conf.api_key, conf.base_url, timeout=conf.timeout, transport=_ProjectTransport(_make_transport()))
    except ValueError as error:
        raise ImproperlyConfigured(f"SMSTunnel: {error}") from error


@receiver(setting_changed, dispatch_uid="django_smstunnel.settings_changed")
def _settings_changed(*, setting: str, **kwargs: Any) -> None:
    if setting.startswith("SMSTUNNEL_"):
        get_client.cache_clear()


def send_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = None,
    default_country_code: str | None = None,
    idempotency_key: str | uuid.UUID | None = None,
) -> SendResult:
    """Sends one text to one number, from your own phone.

    :param to: the number in international format, e.g. ``"+14155550123"``.
    :param message: up to 1600 characters.
    :param device_id: send through this phone; default ``SMSTUNNEL_DEVICE_ID``, else SMSTunnel picks one of yours.
    :param default_country_code: e.g. ``"+40"``, completes a number written without ``+`` or ``00``; default
        ``SMSTUNNEL_DEFAULT_COUNTRY_CODE``.
    :param idempotency_key: your own id for this text, such as ``f"order-{order.pk}-shipped"``. When the same
        key comes again within 24 hours (a retry after a timeout, a task that ran twice), SMSTunnel answers
        with the first result instead of sending a second text.
    :returns: the result; ``result.queued`` is ``True`` when the phone was offline and sends it once it is back.
    :raises smstunnel.SmsTunnelError: when the text was not accepted (see the README for the subclasses).
    :raises ImproperlyConfigured: when ``SMSTUNNEL_API_KEY`` is missing.
    :raises ValueError: when the number or the text is empty, or the idempotency key can't be used.
    """
    key = _checked_key(idempotency_key)
    conf = get_settings()
    client = get_client()
    with _sending_with(key):
        return client.send_sms(
            to,
            message,
            device_id=_or(device_id, conf.device_id),
            default_country_code=_or(default_country_code, conf.default_country_code),
        )


def send_sign_in_code(
    to: str,
    message: str,
    *,
    device_id: str | None = None,
    default_country_code: str | None = None,
    idempotency_key: str | uuid.UUID | None = None,
) -> SendResult:
    """Sends a sign-in or verification code, from your own phone.

    The text must contain the code: 4 to 10 digits standing on their own (``"Your code is 482913"``, or
    ``123-456``); SMSTunnel refuses it otherwise (400). Up to 320 characters. Codes count against a separate
    quota. The arguments are the same as :func:`send_sms`.
    """
    key = _checked_key(idempotency_key)
    conf = get_settings()
    client = get_client()
    with _sending_with(key):
        return client.send_verification_code(
            to,
            message,
            device_id=_or(device_id, conf.device_id),
            default_country_code=_or(default_country_code, conf.default_country_code),
        )


async def asend_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = None,
    default_country_code: str | None = None,
    idempotency_key: str | uuid.UUID | None = None,
) -> SendResult:
    """:func:`send_sms` for async views and tasks (it runs in a worker thread)."""
    return await sync_to_async(send_sms, thread_sensitive=False)(
        to, message, device_id=device_id, default_country_code=default_country_code, idempotency_key=idempotency_key
    )


async def asend_sign_in_code(
    to: str,
    message: str,
    *,
    device_id: str | None = None,
    default_country_code: str | None = None,
    idempotency_key: str | uuid.UUID | None = None,
) -> SendResult:
    """:func:`send_sign_in_code` for async views and tasks (it runs in a worker thread)."""
    return await sync_to_async(send_sign_in_code, thread_sensitive=False)(
        to, message, device_id=device_id, default_country_code=default_country_code, idempotency_key=idempotency_key
    )


def is_retryable(error: BaseException) -> bool:
    """``True`` when the same send may work later: too many requests (429), a send with the same idempotency key
    still running (409), an error on SMSTunnel's side (5xx), or no answer at all.

    Retry a send only when it carries an ``idempotency_key``: after a timeout or a 5xx the first attempt may
    already have sent the text, and the key is what stops the retry from sending it again.
    """
    if isinstance(error, TransportError):
        return True
    if isinstance(error, ApiError):
        if error.status_code == 409:
            return error.error_code == "IDEMPOTENCY_KEY_IN_PROGRESS"
        return error.status_code == 429 or error.status_code >= 500
    return False


def retry_after(error: BaseException) -> float | None:
    """The seconds SMSTunnel asked to wait before trying again (its ``Retry-After`` header), or ``None``."""
    if isinstance(error, RateLimitError):
        return error.retry_after
    if isinstance(error, ApiError):
        value = error.headers.get("retry-after", "").strip()
        try:
            return max(0.0, float(value)) if value else None
        except ValueError:
            return None
    return None


@contextmanager
def _sending_with(key: str | None) -> Iterator[None]:
    token = _idempotency_key.set(key)
    try:
        yield
    finally:
        _idempotency_key.reset(token)


def _checked_key(value: str | uuid.UUID | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(f"idempotency_key must be a string (got {type(value).__name__}).")
    key = value.strip()
    if not key or len(key) > IDEMPOTENCY_KEY_MAX_LENGTH or not key.isascii() or not key.isprintable():
        raise ValueError(
            "idempotency_key must be 1 to 255 printable ASCII characters, such as a UUID or "
            f'"order-1042-shipped" (got {value!r}).'
        )
    return key


def _or(value: str | None, default: str | None) -> str | None:
    return value if value is not None else default
