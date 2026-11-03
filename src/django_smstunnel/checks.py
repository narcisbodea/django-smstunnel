"""System checks: ``manage.py check`` (and ``runserver``) say what is wrong with the ``SMSTUNNEL_*`` settings."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from django.apps import AppConfig
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
