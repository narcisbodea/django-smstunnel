from __future__ import annotations

from typing import Any

from django.conf import settings
from django.contrib.humanize.templatetags.humanize import naturaltime
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.utils.translation import gettext, gettext_lazy

from ..signin import DEFAULT_TEMPLATE, CodeResult, code_text, send_code, validate_international

try:
    from django_otp.models import CooldownMixin, GenerateNotAllowed, SideChannelDevice, ThrottlingMixin, TimestampMixin
except ImportError as error:
    raise ImproperlyConfigured('django_smstunnel.otp needs django-otp: pip install "django-smstunnel[otp]"') from error

__all__ = ["CodeNotSentError", "SmsTunnelDevice"]

DEFAULT_TOKEN_VALIDITY = 300
DEFAULT_COOLDOWN = 60
DEFAULT_THROTTLE_FACTOR = 1


class CodeNotSentError(Exception):
    """The code did not go out. Its message is safe to show to the person signing in."""


class SmsTunnelDevice(TimestampMixin, CooldownMixin, ThrottlingMixin, SideChannelDevice):  # type: ignore[misc,valid-type,unused-ignore]
    """A django-otp device that texts each code from your own phone, through SMSTunnel.

    A text message is a weaker factor than an authenticator app or a passkey. Offer it as an optional factor, never as
    the only one, and never as the way to recover a password.

    Settings: ``SMSTUNNEL_OTP_TOKEN_VALIDITY`` (seconds a code works, default 300), ``SMSTUNNEL_OTP_COOLDOWN`` (seconds
    between two codes, default 60) and ``SMSTUNNEL_OTP_THROTTLE_FACTOR`` (the back-off after wrong codes, default 1).

    .. attribute:: number

        The number the codes go to, in international format.
    """

    number = models.CharField(
        max_length=16,
        validators=[validate_international],
        help_text=gettext_lazy("The number the codes go to, in international format, such as +14155550123."),
    )

    #: The text of the code. Override the template in your project, or set this in a proxy model.
    template_name = DEFAULT_TEMPLATE

    class Meta:
        verbose_name = gettext_lazy("SMSTunnel device")
        verbose_name_plural = gettext_lazy("SMSTunnel devices")

    def generate_challenge(self) -> str:
        """Makes a new code and texts it.

        :returns: a message for the person signing in.
        :raises CodeNotSentError: when the code did not go out, with a message safe to show. django-otp's forms show it.
        """
        allowed, details = self.generate_is_allowed()
        if not allowed:
            if details and details.get("reason") == GenerateNotAllowed.COOLDOWN_DURATION_PENDING:
                return gettext("A code was just sent. You can ask for a new one %(when)s.") % {
                    "when": naturaltime(details["next_generation_at"])
                }
            return gettext("A code can't be sent right now.")

        self.cooldown_set(commit=False)
        self.generate_token(valid_secs=_setting("SMSTUNNEL_OTP_TOKEN_VALIDITY", DEFAULT_TOKEN_VALIDITY), commit=True)

        result = send_code(self.number, code_text(self.token, template_name=self.template_name))
        if result is CodeResult.SENT:
            return gettext("sent by text message")
        if result is CodeResult.UNCERTAIN:
            # It may have gone out: the code stays valid, and no second text is sent.
            return gettext("The code may have been sent. If it doesn't arrive, ask for a new one in a minute.")
        if result is CodeResult.LIMITED:
            raise CodeNotSentError(gettext("Too many codes were requested. Try again later."))
        raise CodeNotSentError(gettext("The code could not be sent. Try again later."))

    def verify_token(self, token: Any) -> bool:
        """Checks ``token``, with django-otp's back-off after wrong codes. A code works once."""
        allowed, _ = self.verify_is_allowed()
        if not allowed:
            return False
        verified: bool = super().verify_token(token)
        if verified:
            self.throttle_reset(commit=False)
            self.set_last_used_timestamp(commit=False)
            self.save()
        else:
            self.throttle_increment()
        return verified

    def get_cooldown_duration(self) -> int:
        """``SMSTUNNEL_OTP_COOLDOWN``: seconds before a new code can be sent."""
        return _setting("SMSTUNNEL_OTP_COOLDOWN", DEFAULT_COOLDOWN)

    def get_throttle_factor(self) -> int:
        """``SMSTUNNEL_OTP_THROTTLE_FACTOR``: seconds to wait after a wrong code, doubling each time."""
        return _setting("SMSTUNNEL_OTP_THROTTLE_FACTOR", DEFAULT_THROTTLE_FACTOR)


def _setting(name: str, default: int) -> int:
    value = getattr(settings, name, default)
    return default if value is None else int(value)
