"""A `django-phone-verify <https://github.com/CuriousLearner/django-phone-verify>`_ backend that sends the codes from
your own phone.

.. code-block:: python

    # settings.py
    PHONE_VERIFICATION = {
        "BACKEND": "django_smstunnel.phone_verify.SmsTunnelBackend",
        "OPTIONS": {},
        "TOKEN_LENGTH": 6,
        "MESSAGE": "Your {app} code is {security_code}",
        "APP_NAME": "Example",
        "SECURITY_CODE_EXPIRATION_SECONDS": 600,
        "VERIFY_SECURITY_CODE_ONLY_ONCE": True,
    }

Add ``django_smstunnel.signin.ClientIPMiddleware`` to ``MIDDLEWARE`` too: django-phone-verify's register endpoint is
public and takes any number, and this is what lets the limit per IP apply to it.
"""

from __future__ import annotations

from typing import Any, NoReturn

from smstunnel import SmsTunnelError

from .signin import number_text, send_code, sending_in_background

try:
    from phone_verify.backends.base import BaseBackend
except ImportError as error:
    raise ImportError(
        'django_smstunnel.phone_verify needs django-phone-verify: pip install "django-smstunnel[phone-verify]"'
    ) from error

__all__ = ["SmsTunnelBackend"]


class SmsTunnelBackend(BaseBackend):  # type: ignore[misc,valid-type,unused-ignore]
    """Sends django-phone-verify's texts through SMSTunnel's sign-in code route.

    A failed send is logged here without the number and not raised: django-phone-verify would otherwise write the
    number to its own log.
    """

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.exception_class = SmsTunnelError

    def send_sms(self, number: Any, message: str) -> None:
        """Called by django-phone-verify: texts ``message`` to ``number``."""
        send_code(number_text(number), message, background=sending_in_background())

    def send_bulk_sms(self, numbers: Any, message: str) -> NoReturn:
        """Refused: SMSTunnel sends one text to one number. django-phone-verify itself never calls this."""
        raise NotImplementedError("SMSTunnel sends one text per number. Call send_sms() once for each.")
