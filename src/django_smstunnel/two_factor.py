"""Sign-in codes for `django-two-factor-auth <https://github.com/jazzband/django-two-factor-auth>`_, sent as texts
from your own phone, from your own number.

.. code-block:: python

    # settings.py
    TWO_FACTOR_SMS_GATEWAY = "django_smstunnel.two_factor.SmsTunnel"

django-two-factor-auth then offers "Text message" as a sign-in method (with its phone number plugin,
``two_factor.plugins.phonenumber``, in ``INSTALLED_APPS``). Phone calls are not supported: leave
``TWO_FACTOR_CALL_GATEWAY`` unset.
"""

from __future__ import annotations

from typing import Any, NoReturn

from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils.translation import gettext

from .client import send_sign_in_code

__all__ = ["SmsTunnel"]


class SmsTunnel:
    """Sends django-two-factor-auth's codes through SMSTunnel.

    The text comes from the template ``django_smstunnel/two_factor/sms_message.txt`` ("Your OTP token is
    123456", in the user's language when django-two-factor-auth has a translation). Override the template in your
    project to change it; it gets ``token`` and must keep the code on its own (4 to 10 digits).

    It sends with the ``SMSTUNNEL_*`` settings: ``SMSTUNNEL_DEVICE_ID`` picks the phone, and
    ``SMSTUNNEL_DEFAULT_COUNTRY_CODE`` completes numbers saved without a country code.
    """

    template_name = "django_smstunnel/two_factor/sms_message.txt"

    def send_sms(self, device: Any, token: str) -> None:
        """Called by django-two-factor-auth: texts ``token`` to ``device.number``."""
        send_sign_in_code(
            _number_of(device), self.render_message(token), idempotency_key=self.idempotency_key(device, token)
        )

    def idempotency_key(self, device: Any, token: str) -> str | None:
        """``None``: every request sends a text. Override it to return a key (for example from the device and the
        token) when a second request for the same code within 24 hours should not send a second text."""
        return None

    def make_call(self, device: Any, token: str) -> NoReturn:
        """SMSTunnel cannot make phone calls."""
        raise NotImplementedError(
            "SMSTunnel sends texts and cannot make phone calls. Leave TWO_FACTOR_CALL_GATEWAY unset, or set it to a "
            "provider that makes calls."
        )

    def render_message(self, token: str) -> str:
        """The text for ``token``, from :attr:`template_name`."""
        try:
            text: str = render_to_string(self.template_name, {"token": token})
        except TemplateDoesNotExist:
            if self.template_name != SmsTunnel.template_name:
                raise
            # django_smstunnel is not in INSTALLED_APPS: the same words, without the template.
            text = gettext("Your OTP token is %(token)s") % {"token": token}
        return text.strip()


def _number_of(device: Any) -> str:
    number = device.number
    e164 = getattr(number, "as_e164", None)
    if isinstance(e164, str) and e164:
        return e164
    return str(number or "")
