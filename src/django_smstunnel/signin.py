"""What the sign-in integrations share: one careful way to send a code.

django-allauth (:mod:`django_smstunnel.allauth`), django-otp (:mod:`django_smstunnel.otp`) and django-phone-verify
(:mod:`django_smstunnel.phone_verify`) all send through :func:`send_code`. For each code it:

- refuses a number that is not in international format (``+`` and the country code), before any request;
- counts the code against the limits per IP, per number and for the whole project (``SMSTUNNEL_SIGNIN_LIMITS``), so
  a public form can't be used to drain your SIM;
- sends it once through ``POST /api/v1/sms/send-2fa``, with an ``Idempotency-Key``, and never retries on its own;
- logs what went wrong without the number, the code, the text or the key.

``import django_smstunnel`` does not load this module: a project that uses none of these integrations runs exactly as
before.
"""

from __future__ import annotations

import atexit
import enum
import ipaddress
import logging
import os
import queue
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.validators import RegexValidator
from django.db import connections
from django.http import HttpRequest
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils.crypto import salted_hmac
from django.utils.module_loading import import_string
from django.utils.translation import gettext, gettext_lazy
from smstunnel import ApiError, SmsTunnelError

from .client import send_sign_in_code

__all__ = [
    "DEFAULT_LIMITS",
    "DEFAULT_TEMPLATE",
    "ClientIPMiddleware",
    "CodeResult",
    "Rate",
    "client_ip",
    "code_text",
    "is_international",
    "limits",
    "number_text",
    "send_code",
    "sending_in_background",
    "validate_international",
]

logger = logging.getLogger("django_smstunnel.signin")

DEFAULT_TEMPLATE = "django_smstunnel/signin/code.txt"

#: Per IP, per number and for the whole project. A value is one or more rates such as ``"5/h,10/d"``.
DEFAULT_LIMITS: Mapping[str, str] = {"ip": "20/h,100/d", "number": "5/h,10/d", "total": "100/h,300/d"}

_INTERNATIONAL = re.compile(r"\+[1-9][0-9]{6,14}")
_RATE = re.compile(r"([0-9]+)/([0-9]*)([smhd])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

validate_international = RegexValidator(
    regex=r"^\+[1-9][0-9]{6,14}\Z",
    message=gettext_lazy("Enter the number in international format: + and the country code, such as +14155550123."),
    code="invalid_phone",
)

# The visitor's IP, for code that sends without seeing the request (ClientIPMiddleware sets it).
_client_ip: ContextVar[str | None] = ContextVar("django_smstunnel_client_ip", default=None)
_warned_about_proxy = False

# Sending from public forms: a few threads and a short queue per process, so a flood can't pile up threads or memory.
_WORKERS = 4
_QUEUE_SIZE = 32
_FULL_LOG_EVERY = 60.0
_EXIT_WAIT = 5.0


class CodeResult(enum.Enum):
    """What happened to a code handed to :func:`send_code`."""

    #: SMSTunnel accepted the text.
    SENT = "sent"
    #: Handed to a background thread; how it ended only reaches the log.
    BACKGROUND = "background"
    #: The number is not in international format. Nothing was sent.
    BAD_NUMBER = "bad_number"
    #: A limit was reached, or too many codes are already waiting to go out. Nothing was sent.
    LIMITED = "limited"
    #: SMSTunnel refused the text, or the setup is wrong. Nothing was sent.
    FAILED = "failed"
    #: No clear answer (a timeout, a lost connection, a 5xx). The text may have gone out, so it is not sent again.
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class Rate:
    """At most ``amount`` codes per ``seconds``."""

    amount: int
    seconds: int


def is_international(number: object) -> bool:
    """``True`` for ``+`` and 7 to 15 digits, the first one not 0."""
    return isinstance(number, str) and _INTERNATIONAL.fullmatch(number) is not None


def number_text(value: Any) -> str:
    """A stored number as text: a ``phonenumber_field`` ``PhoneNumber`` gives its international form."""
    e164 = getattr(value, "as_e164", None)
    if isinstance(e164, str) and e164:
        return e164
    return str(value or "")


def code_text(code: str, *, template_name: str = DEFAULT_TEMPLATE) -> str:
    """The text for ``code``, from the template (override it in your project to change the words)."""
    try:
        text: str = render_to_string(template_name, {"code": code})
    except TemplateDoesNotExist:
        if template_name != DEFAULT_TEMPLATE:
            raise
        # django_smstunnel is not in INSTALLED_APPS: the same words, without the template.
        text = gettext("Your sign-in code is %(code)s") % {"code": code}
    return text.strip()


def sending_in_background() -> bool:
    """``SMSTUNNEL_SIGNIN_BACKGROUND``: public forms answer before the send, so their timing says nothing."""
    return bool(getattr(settings, "SMSTUNNEL_SIGNIN_BACKGROUND", True))


def limits() -> dict[str, tuple[Rate, ...]]:
    """``SMSTUNNEL_SIGNIN_LIMITS`` over :data:`DEFAULT_LIMITS`. ``None`` as a value turns that limit off.

    :raises ImproperlyConfigured: when the setting can't be read.
    """
    configured = getattr(settings, "SMSTUNNEL_SIGNIN_LIMITS", None)
    if configured is None:
        configured = {}
    if not isinstance(configured, Mapping):
        raise ImproperlyConfigured(
            'SMSTUNNEL_SIGNIN_LIMITS must be a dict such as {"number": "5/h,10/d", "ip": "20/h", "total": "100/h"}.'
        )
    unknown = sorted(str(name) for name in set(configured) - set(DEFAULT_LIMITS))
    if unknown:
        raise ImproperlyConfigured(
            f"SMSTUNNEL_SIGNIN_LIMITS has an unknown limit ({', '.join(unknown)}): use number, ip or total."
        )
    return {scope: _parse_rates(scope, configured.get(scope, default)) for scope, default in DEFAULT_LIMITS.items()}


def _parse_rates(scope: str, value: object) -> tuple[Rate, ...]:
    if value is None or value == "":
        return ()
    if not isinstance(value, str):
        raise ImproperlyConfigured(f'SMSTUNNEL_SIGNIN_LIMITS["{scope}"] must be text such as "5/h,10/d", or None.')
    rates = []
    for part in value.split(","):
        match = _RATE.fullmatch(part.strip())
        seconds = int(match[2] or 1) * _UNITS[match[3]] if match else 0
        if match is None or seconds <= 0:
            raise ImproperlyConfigured(
                f'SMSTUNNEL_SIGNIN_LIMITS["{scope}"] has a rate it can\'t read ({part.strip()!r}): write it like '
                '"5/h", "10/d" or "3/15m".'
            )
        rates.append(Rate(int(match[1]), seconds))
    return tuple(rates)


def send_code(to: str, text: str, *, ip: str | None = None, background: bool = False) -> CodeResult:
    """Sends one sign-in code to one number. It never raises, so a failed send can't change a public page.

    :param to: the number, ``+`` and the country code.
    :param text: the text, with the code as 4 to 10 digits standing on their own.
    :param ip: the visitor's IP; default the one :class:`ClientIPMiddleware` saw. Without one, only the limits per
        number and for the project apply.
    :param background: hand it to the worker threads and return at once. When too many codes are already waiting,
        nothing is sent and the site owner finds it in the log.
    """
    if not is_international(to):
        logger.warning("SMSTunnel: a sign-in code was not sent: the number is not in international format.")
        return CodeResult.BAD_NUMBER
    address = ip if ip is not None else _client_ip.get()
    if not background:
        return _limit_and_deliver(to, text, address)
    # The counting happens in the worker too: the cache is a network call on most sites, and a number with an account
    # must take no longer to answer than one without.
    return _pool.submit(to, text, address)


class _SendPool:
    """A fixed set of worker threads reading a bounded queue."""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, str, str | None]] = queue.Queue(_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._dropped = 0
        self._last_report = -_FULL_LOG_EVERY

    def submit(self, to: str, text: str, ip: str | None) -> CodeResult:
        """Queues one code without ever waiting. When the queue is full, nothing is sent."""
        if not self._has_workers():
            logger.error("SMSTunnel: a sign-in code was not sent: no thread could be started.")
            return CodeResult.FAILED
        try:
            self._queue.put_nowait((to, text, ip))
        except queue.Full:
            self._report_full()
            return CodeResult.LIMITED
        return CodeResult.BACKGROUND

    def drain(self, timeout: float) -> bool:
        """Waits up to ``timeout`` seconds for the queued codes. ``True`` when none is left."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _has_workers(self) -> bool:
        with self._lock:
            self._workers = [worker for worker in self._workers if worker.is_alive()]
            while len(self._workers) < _WORKERS:
                worker = threading.Thread(target=self._work, name="django-smstunnel-code", daemon=True)
                try:
                    worker.start()
                except RuntimeError:  # The process is shutting down, or can't start more threads.
                    break
                self._workers.append(worker)
            return bool(self._workers)

    def _work(self) -> None:
        while True:
            to, text, ip = self._queue.get()
            try:
                try:
                    _limit_and_deliver(to, text, ip)
                finally:
                    connections.close_all()  # A database cache opens a connection for this thread.
            except Exception as error:
                logger.error("SMSTunnel: a sign-in code was not sent (%s).", type(error).__name__)
            finally:
                self._queue.task_done()

    def _report_full(self) -> None:
        """At most one line a minute: a flood must not fill the log as well."""
        with self._lock:
            self._dropped += 1
            now = time.monotonic()
            if now - self._last_report < _FULL_LOG_EVERY:
                return
            dropped, self._dropped, self._last_report = self._dropped, 0, now
        logger.error(
            "SMSTunnel: %s sign-in code(s) were not sent: too many are already waiting to go out. Check that SMSTunnel "
            "answers, and whether someone is flooding your sign-in form.",
            dropped,
        )


_pool = _SendPool()


def _new_pool_after_fork() -> None:
    """A forked process has no worker threads, and must not send the codes its parent still has queued."""
    global _pool
    _pool = _SendPool()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_new_pool_after_fork)
atexit.register(lambda: _pool.drain(_EXIT_WAIT))


def _limit_and_deliver(to: str, text: str, ip: str | None) -> CodeResult:
    try:
        reached = _limit_reached(to, ip)
    except _CounterNotKeptError:
        logger.error(
            "SMSTunnel: a sign-in code was not sent: the cache keeps nothing (DummyCache?), so the limits can't be "
            "counted. Use a real cache: Redis, Memcached or the database."
        )
        return CodeResult.FAILED
    except Exception as error:
        # With no way to count, the safe side is to send nothing: an open form could drain the SIM.
        logger.error(  # No traceback: it could carry the number.
            "SMSTunnel: a sign-in code was not sent: the limits could not be checked (%s).", type(error).__name__
        )
        return CodeResult.FAILED
    if reached is not None:
        logger.warning("SMSTunnel: a sign-in code was not sent: the limit per %s was reached.", reached)
        return CodeResult.LIMITED
    return _deliver(to, text, _idempotency_key(to, text))


def _deliver(to: str, text: str, key: str) -> CodeResult:
    """One request, never repeated. Only the kind of failure is logged: a server message could quote the number."""
    try:
        send_sign_in_code(to, text, idempotency_key=key)
    except ApiError as error:
        uncertain = error.status_code >= 500 or error.error_code == "IDEMPOTENCY_KEY_IN_PROGRESS"
        logger.error(
            "SMSTunnel: a sign-in code %s (HTTP %s%s): %s",
            "may not have gone out, and is not sent again" if uncertain else "was not sent",
            error.status_code,
            f", {error.error_code}" if error.error_code else "",
            _reason(error),
        )
        return CodeResult.UNCERTAIN if uncertain else CodeResult.FAILED
    except SmsTunnelError:
        logger.error("SMSTunnel: no answer for a sign-in code. It may have gone out, so it is not sent again.")
        return CodeResult.UNCERTAIN
    except Exception as error:
        logger.error("SMSTunnel: a sign-in code was not sent (%s). Run manage.py check.", type(error).__name__)
        return CodeResult.FAILED
    return CodeResult.SENT


def _reason(error: ApiError) -> str:
    status = error.status_code
    if status == 401:
        return "the API key is wrong, was deleted, or its account is suspended"
    if status == 403:
        return "the key lacks a permission, or the plan does not include this"
    if status == 429:
        return "too many texts, or the plan's daily allowance is used up"
    if status == 400:
        return "SMSTunnel refused the number or the text (it must hold a code of 4 to 10 digits)"
    if status == 409:
        return "the same code is already being sent"
    if status >= 500:
        return "an error on SMSTunnel's side"
    return "SMSTunnel refused the text"


def _idempotency_key(to: str, text: str) -> str:
    """The same for the same text to the same number, so a repeat of one send can't text twice. It reveals neither."""
    digest = salted_hmac("django_smstunnel.signin.idempotency", f"{to}\n{text}", algorithm="sha256").hexdigest()
    return f"signin-{digest[:48]}"


def _limit_reached(to: str, ip: str | None) -> str | None:
    """Counts one code. Returns the limit it went over, or ``None``.

    In this order, so that a flood from one IP doesn't use up the allowance of the number it targets, and a number
    over its limit doesn't use up the project's.
    """
    rates = limits()
    network = _network(ip) if ip else None
    targets = [("number", to), ("total", "all")]
    if network is not None:
        targets.insert(0, ("ip", network))
    for scope, value in targets:
        name = salted_hmac(f"django_smstunnel.signin.{scope}", value, algorithm="sha256").hexdigest()[:32]
        for rate in rates[scope]:
            if not _count(scope, name, rate):
                return scope
    return None


class _CounterNotKeptError(Exception):
    """The cache did not keep a counter, so no limit can be enforced."""


def _count(scope: str, name: str, rate: Rate) -> bool:
    now = int(time.time())
    window = now // rate.seconds
    key = f"django_smstunnel:signin:{scope}:{name}:{rate.amount}:{rate.seconds}:{window}"
    timeout = rate.seconds - now % rate.seconds + 1
    cache.add(key, 0, timeout)
    try:
        count = cache.incr(key)
    except ValueError:  # It expired between the two calls, or this cache keeps nothing.
        cache.set(key, 1, timeout)
        stored = cache.get(key)
        if stored is None:
            raise _CounterNotKeptError from None
        count = int(stored)
    return bool(count <= rate.amount)


def client_ip(request: HttpRequest, *, candidate: str | None = None) -> str | None:
    """The visitor's IP for the per-IP limit, or ``None`` when it can't be known.

    ``SMSTUNNEL_CLIENT_IP`` (a function or its dotted path, taking the request) decides when it is set. Otherwise this
    is ``candidate`` or ``REMOTE_ADDR``. A request from a private address with ``X-Forwarded-For`` gets ``None``, which
    turns the per-IP limit off: ``REMOTE_ADDR`` may be a proxy's, the same for every visitor, so counting it could lock
    everyone out at once, and the header can't be trusted, because anyone on that network can write it. The limits per
    number and for the project don't depend on any of this.
    """
    resolver = getattr(settings, "SMSTUNNEL_CLIENT_IP", None)
    if resolver is not None:
        try:
            function = import_string(resolver) if isinstance(resolver, str) else resolver
            value = function(request)
        except Exception as error:
            logger.error(
                "SMSTunnel: SMSTUNNEL_CLIENT_IP failed (%s), so the per-IP limit is skipped.", type(error).__name__
            )
            return None
        return _network(value) if isinstance(value, str) and value else None
    remote = str(request.META.get("REMOTE_ADDR") or "")
    address = candidate or remote
    if address == remote and request.META.get("HTTP_X_FORWARDED_FOR") and _is_private(remote):
        _warn_about_proxy()
        return None
    return _network(address) if address else None


def _is_private(address: str) -> bool:
    """Not a public address. A public IPv4 visitor on a dual-stack socket arrives as ``::ffff:a.b.c.d``."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return not parsed.is_global


def _network(value: str) -> str | None:
    """The address to count: an IPv6 visitor controls a whole /64, so that is what counts as one."""
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network((address, 64), strict=False).network_address)
    return str(address)


def _warn_about_proxy() -> None:
    global _warned_about_proxy
    if not _warned_about_proxy:
        _warned_about_proxy = True
        logger.warning(
            "SMSTunnel: the per-IP limit on sign-in codes is OFF. Requests arrive from a private address with an "
            "X-Forwarded-For header, which this app can't verify. It stays off until you set SMSTUNNEL_CLIENT_IP to a "
            "function that returns the visitor's IP (with django-allauth, ALLAUTH_TRUSTED_PROXY_COUNT also works). The "
            "limits per number and for the whole project still apply."
        )


class ClientIPMiddleware:
    """Remembers the visitor's IP for the per-IP limit, for integrations whose host doesn't pass the request on
    (django-phone-verify, django-otp). django-allauth does not need it."""

    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[HttpRequest], Any]) -> None:
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> Any:
        if self._is_async:
            return self._acall(request)
        token = _client_ip.set(client_ip(request))
        try:
            return self.get_response(request)
        finally:
            _client_ip.reset(token)

    async def _acall(self, request: HttpRequest) -> Any:
        token = _client_ip.set(client_ip(request))
        try:
            response: Awaitable[Any] = self.get_response(request)
            return await response
        finally:
            _client_ip.reset(token)
