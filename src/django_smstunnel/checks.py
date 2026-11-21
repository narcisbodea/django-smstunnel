"""System checks: ``manage.py check`` (and ``runserver``) say what is wrong with the ``SMSTUNNEL_*`` settings."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from django.apps import AppConfig, apps
from django.conf import settings
from django.core.checks import CheckMessage, Error, Warning, register
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from . import conf

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


@register()
def check_settings(app_configs: Sequence[AppConfig] | None = None, **kwargs: Any) -> list[CheckMessage]:
    messages: list[CheckMessage] = []

    try:
        conf.api_key()
    except conf.MissingSetting:
        messages.append(
            Warning(
                "SMSTUNNEL_API_KEY is not set, so no text can be sent.",
                hint=(
                    "Create a key in your SMSTunnel dashboard (https://dashboard.smstunnel.io/apps, Apps → Add an "
                    "app), keep it in an environment variable and read it in settings.py: "
                    'SMSTUNNEL_API_KEY = os.environ.get("SMSTUNNEL_API_KEY", "")'
                ),
                id="smstunnel.W001",
            )
        )
    except ImproperlyConfigured as error:
        messages.append(Error(str(error), id="smstunnel.E001"))

    readers: tuple[Callable[[], object], ...] = (conf.timeout, conf.device_id, conf.default_country_code)
    for read in readers:
        try:
            read()
        except ImproperlyConfigured as error:
            messages.append(Error(str(error), id="smstunnel.E001"))

    try:
        url = urlsplit(conf.base_url())
    except ImproperlyConfigured as error:
        messages.append(Error(str(error), id="smstunnel.E001"))
    else:
        host = (url.hostname or "").lower()
        if url.scheme.lower() == "http" and host not in _LOCAL_HOSTS and not host.endswith(".localhost"):
            messages.append(
                Warning(
                    "SMSTUNNEL_BASE_URL starts with http://, so the API key would travel unencrypted.",
                    hint='Use "https://smstunnel.io" (the default): leave SMSTUNNEL_BASE_URL unset.',
                    id="smstunnel.W003",
                )
            )

    if _points_at_us(getattr(settings, "TWO_FACTOR_CALL_GATEWAY", None)):
        messages.append(
            Warning(
                "TWO_FACTOR_CALL_GATEWAY points at SMSTunnel, which sends texts and cannot make phone calls.",
                hint=(
                    "Leave TWO_FACTOR_CALL_GATEWAY unset (or set it to a provider that makes calls) and keep "
                    'TWO_FACTOR_SMS_GATEWAY = "django_smstunnel.two_factor.SmsTunnel".'
                ),
                id="smstunnel.W002",
            )
        )

    return messages


def _points_at_us(path: object) -> bool:
    if not isinstance(path, str) or not path.strip():
        return False
    from .two_factor import SmsTunnel

    try:
        target = import_string(path.strip())
    except ImportError:
        return False
    return isinstance(target, type) and issubclass(target, SmsTunnel)


@register()
def check_signin(app_configs: Sequence[AppConfig] | None = None, **kwargs: Any) -> list[CheckMessage]:
    """The sign-in integrations. Says nothing to a project that uses none of them."""
    adapter = _allauth_adapter()
    backend = _phone_verify_backend()
    if not (
        adapter or backend or apps.is_installed("django_smstunnel.otp") or hasattr(settings, "SMSTUNNEL_SIGNIN_LIMITS")
    ):
        return []

    from .signin import limits

    messages: list[CheckMessage] = []
    rates: dict[str, Any] = {}
    try:
        rates = limits()
    except ImproperlyConfigured as error:
        messages.append(Error(str(error), id="smstunnel.E002"))

    cache_backend = str(settings.CACHES.get("default", {}).get("BACKEND", ""))
    if cache_backend.endswith("DummyCache") and any(rates.values()):
        messages.append(
            Warning(
                "The default cache is DummyCache, which keeps nothing, so the sign-in code limits can't be counted "
                "and no sign-in code will be sent.",
                hint="Use a real cache, shared between your workers (Redis, Memcached or the database).",
                id="smstunnel.W006",
            )
        )

    if adapter is not None:
        messages.extend(_allauth_messages(adapter))
    if backend is not None:
        messages.extend(_phone_verify_messages())
    return messages


def _allauth_adapter() -> type | None:
    path = getattr(settings, "ACCOUNT_ADAPTER", None)
    if not isinstance(path, str) or not path.strip():
        return None
    from .allauth import SmsTunnelAccountAdapterMixin

    try:
        target = import_string(path.strip())
    except Exception:  # Whatever is wrong there is not for this check to raise.
        return None
    return target if isinstance(target, type) and issubclass(target, SmsTunnelAccountAdapterMixin) else None


def _allauth_messages(adapter: type) -> list[CheckMessage]:
    from allauth.account import app_settings
    from allauth.account.adapter import DefaultAccountAdapter

    from .allauth import SmsTunnelAccountAdapterMixin, code_format_problem

    messages: list[CheckMessage] = []
    order = adapter.__mro__
    if DefaultAccountAdapter in order and order.index(DefaultAccountAdapter) < order.index(
        SmsTunnelAccountAdapterMixin
    ):
        messages.append(
            Error(
                f"{adapter.__name__} lists DefaultAccountAdapter before SmsTunnelAccountAdapterMixin, so no sign-in "
                "code would be sent.",
                hint="Put the mixin first: class AccountAdapter(SmsTunnelAccountAdapterMixin, DefaultAccountAdapter).",
                id="smstunnel.E003",
            )
        )
    if not app_settings.PREVENT_ENUMERATION:
        messages.append(
            Warning(
                "ACCOUNT_PREVENT_ENUMERATION is off, so the sign-in form says whether a phone number has an account.",
                hint="Remove the setting (it is on by default).",
                id="smstunnel.W004",
            )
        )
    problem = code_format_problem()
    if problem:
        messages.append(
            Warning(
                f"The format of phone codes doesn't suit a text: {problem}.",
                hint='SMSTunnel needs a code of 4 to 10 digits: {"numeric": True, "length": 6, "dashed": False}.',
                id="smstunnel.W005",
            )
        )
    return messages


def _phone_verify_backend() -> type | None:
    config = getattr(settings, "PHONE_VERIFICATION", None)
    path = config.get("BACKEND") if isinstance(config, dict) else None
    if not isinstance(path, str) or not path.startswith("django_smstunnel."):
        return None
    try:
        target = import_string(path)
    except Exception:  # The same: a project not using us must see no new failure.
        return None
    return target if isinstance(target, type) else None


def _phone_verify_messages() -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    if "django_smstunnel.signin.ClientIPMiddleware" not in (getattr(settings, "MIDDLEWARE", None) or ()):
        messages.append(
            Warning(
                "django-phone-verify's register endpoint is public, and without ClientIPMiddleware the limit per IP "
                "can't apply to it.",
                hint='Add "django_smstunnel.signin.ClientIPMiddleware" to MIDDLEWARE.',
                id="smstunnel.W007",
            )
        )
    config = getattr(settings, "PHONE_VERIFICATION", None)
    length = config.get("TOKEN_LENGTH") if isinstance(config, dict) else None
    if isinstance(length, int) and not 4 <= length <= 10:
        messages.append(
            Error(
                f"PHONE_VERIFICATION TOKEN_LENGTH is {length}, and SMSTunnel sends a code of 4 to 10 digits.",
                hint="Use a TOKEN_LENGTH of 6 to 10.",
                id="smstunnel.E004",
            )
        )
    return messages
