"""The project's SMSTunnel client, and the functions that send through it."""

from __future__ import annotations

import dataclasses
import functools
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Literal, overload

import django
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver
from django.utils import timezone
from smstunnel import ApiError, Client, RateLimitError, ScheduledText, ScheduledTexts, SendResult, TransportError
from smstunnel.transport import HttpRequest, HttpResponse, Transport, UrllibTransport

from ._version import __version__
from .conf import get_settings

__all__ = [
    "acancel_scheduled",
    "alist_scheduled",
    "asend_sign_in_code",
    "asend_sms",
    "cancel_scheduled",
    "get_client",
    "is_retryable",
    "list_scheduled",
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


@overload
def send_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = ...,
    default_country_code: str | None = ...,
    idempotency_key: str | uuid.UUID | None = ...,
    send_at: None = ...,
) -> SendResult: ...


@overload
def send_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = ...,
    default_country_code: str | None = ...,
    idempotency_key: str | uuid.UUID | None = ...,
    send_at: datetime | str,
) -> ScheduledText: ...


def send_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = None,
    default_country_code: str | None = None,
    idempotency_key: str | uuid.UUID | None = None,
    send_at: datetime | str | None = None,
) -> SendResult | ScheduledText:
    """Sends one text to one number, from your own phone: now, or at ``send_at``.

    :param to: the number in international format, e.g. ``"+14155550123"``.
    :param message: up to 1600 characters.
    :param device_id: send through this phone; default ``SMSTUNNEL_DEVICE_ID``, else SMSTunnel picks one of yours.
    :param default_country_code: e.g. ``"+40"``, completes a number written without ``+`` or ``00``; default
        ``SMSTUNNEL_DEFAULT_COUNTRY_CODE``.
    :param idempotency_key: your own id for this text, such as ``f"order-{order.pk}-shipped"``. When the same
        key comes again within 24 hours (a retry after a timeout, a task that ran twice), SMSTunnel answers
        with the first result instead of sending a second text.
    :param send_at: send it at this time instead of now, 1 minute to 30 days ahead: an aware ``datetime`` (such
        as ``timezone.now() + timedelta(hours=1)`` or a ``DateTimeField`` with ``USE_TZ = True``) or an ISO 8601
        string with an offset. With ``USE_TZ = False``, a naive ``datetime`` is local time, as Django takes it: the
        current time zone. SMSTunnel keeps the text and hands it to your phone then.
    :returns: the result; ``result.queued`` is ``True`` when the phone was offline and sends it once it is back.
        With ``send_at``, a :class:`smstunnel.ScheduledText` instead (``id``, ``send_at``, ``status``).
    :raises smstunnel.SmsTunnelError: when the text was not accepted (see the README for the subclasses).
    :raises ImproperlyConfigured: when ``SMSTUNNEL_API_KEY`` is missing.
    :raises ValueError: when the number or the text is empty, the idempotency key can't be used, or ``send_at`` is
        a naive ``datetime`` while ``USE_TZ`` is on, or a string without an offset.
    """
    key = _checked_key(idempotency_key)
    when = _aware(send_at)
    conf = get_settings()
    client = get_client()
    with _sending_with(key):
        return client.send_sms(
            to,
            message,
            device_id=_or(device_id, conf.device_id),
            default_country_code=_or(default_country_code, conf.default_country_code),
            send_at=when,
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


@overload
async def asend_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = ...,
    default_country_code: str | None = ...,
    idempotency_key: str | uuid.UUID | None = ...,
    send_at: None = ...,
) -> SendResult: ...


@overload
async def asend_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = ...,
    default_country_code: str | None = ...,
    idempotency_key: str | uuid.UUID | None = ...,
    send_at: datetime | str,
) -> ScheduledText: ...


async def asend_sms(
    to: str,
    message: str,
    *,
    device_id: str | None = None,
    default_country_code: str | None = None,
    idempotency_key: str | uuid.UUID | None = None,
    send_at: datetime | str | None = None,
) -> SendResult | ScheduledText:
    """:func:`send_sms` for async views and tasks (it runs in a worker thread)."""
    return await sync_to_async(
        lambda: send_sms(
            to,
            message,
            device_id=device_id,
            default_country_code=default_country_code,
            idempotency_key=idempotency_key,
            send_at=send_at,
        ),
        thread_sensitive=False,
    )()


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


def list_scheduled(
    scope: Literal["waiting", "done"] = "waiting",
    *,
    page: int = 1,
    limit: int = 50,
) -> ScheduledTexts:
    """The texts your project's key scheduled with ``send_sms(..., send_at=...)``: ``"waiting"`` (the default),
    soonest first, or ``"done"``, the ones that went out, failed or were canceled in the last 30 days.

    ``get_client().get_scheduled(scheduled_id)`` reads one. Needs a key with ``sms:read``.
    """
    return get_client().list_scheduled(scope, page=page, limit=limit)


def cancel_scheduled(scheduled_id: str) -> ScheduledText:
    """Cancels a text your project's key scheduled, while it still waits: pass the ``id`` that ``send_sms(...,
    send_at=...)`` returned. SMSTunnel answers 409 (``error_code`` ``SCHEDULE_NOT_EDITABLE``) when it already went out
    or was canceled.
    """
    return get_client().cancel_scheduled(scheduled_id)


async def alist_scheduled(
    scope: Literal["waiting", "done"] = "waiting",
    *,
    page: int = 1,
    limit: int = 50,
) -> ScheduledTexts:
    """:func:`list_scheduled` for async views and tasks (it runs in a worker thread)."""
    return await sync_to_async(list_scheduled, thread_sensitive=False)(scope, page=page, limit=limit)


async def acancel_scheduled(scheduled_id: str) -> ScheduledText:
    """:func:`cancel_scheduled` for async views and tasks (it runs in a worker thread)."""
    return await sync_to_async(cancel_scheduled, thread_sensitive=False)(scheduled_id)


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


def _aware(send_at: datetime | str | None) -> datetime | str | None:
    """``send_at`` as Django means it. With ``USE_TZ = False`` a naive ``datetime`` is local time, so it gets the
    current time zone; with ``USE_TZ = True`` datetimes are aware, and a naive one is refused rather than guessed."""
    if not isinstance(send_at, datetime) or not timezone.is_naive(send_at):
        return send_at
    if settings.USE_TZ:
        raise ValueError(
            "send_at is a naive datetime, but USE_TZ is True, so the moment it means is not known. Use an aware one: "
            "timezone.now() + timedelta(hours=1), a DateTimeField's value, or timezone.make_aware(value)."
        )
    return timezone.make_aware(send_at)


def _or(value: str | None, default: str | None) -> str | None:
    return value if value is not None else default
