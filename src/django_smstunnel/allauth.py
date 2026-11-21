"""Phone sign-in for `django-allauth <https://docs.allauth.org/>`_, with the codes sent from your own phone.

.. code-block:: python

    # myapp/adapter.py
    from allauth.account.adapter import DefaultAccountAdapter
    from django_smstunnel.allauth import SmsTunnelAccountAdapterMixin


    class AccountAdapter(SmsTunnelAccountAdapterMixin, DefaultAccountAdapter):
        ...  # set_phone, get_phone, set_phone_verified and get_user_by_phone: where your project keeps the number

    # settings.py
    ACCOUNT_ADAPTER = "myapp.adapter.AccountAdapter"

The mixin must come before ``DefaultAccountAdapter``. This module doesn't import django-allauth.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from django.http import HttpRequest
from django.utils.crypto import get_random_string

from .signin import DEFAULT_TEMPLATE, client_ip, code_text, send_code, sending_in_background

__all__ = ["DEFAULT_CODE_LENGTH", "SmsTunnelAccountAdapterMixin", "code_format_problem"]

logger = logging.getLogger("django_smstunnel.signin")

DEFAULT_CODE_LENGTH = 6
_SHORTEST_CODE = 4
_LONGEST_CODE = 10
_SEPARATORS = re.compile(r"[-\s]")
_SENDABLE = re.compile(r"[0-9]{4,10}")


class SmsTunnelAccountAdapterMixin:
    """Fills the sending half of django-allauth's phone API; your adapter keeps the four storage methods.

    - Codes are digits only, because SMSTunnel's sign-in route needs a code of 4 to 10 digits and allauth's default has
      letters. They are 6 digits long unless ``ACCOUNT_PHONE_VERIFICATION_CODE_FORMAT`` asks for a numeric length.
    - A number with no account gets no text. A failed send looks exactly like a good one to the visitor (it goes to
      the log instead), and sending happens in the background, so neither the page nor its timing shows whether a
      number has an account.
    """

    #: The text of the code. Override the template in your project, or point this at your own.
    sms_template_name = DEFAULT_TEMPLATE

    def generate_phone_verification_code(self, *, user: Any = None, phone: str | None = None) -> str:
        """A code of digits only (see the class)."""
        return get_random_string(_code_length(), "0123456789")

    def send_verification_code_sms(self, user: Any, phone: str, code: str, **kwargs: Any) -> None:
        """Called by django-allauth: texts ``code`` to ``phone``. It never raises, for the reason in the class."""
        try:
            plain = _SEPARATORS.sub("", code)
            text = code_text(plain if _SENDABLE.fullmatch(plain) else code, template_name=self.sms_template_name)
            ip = self._smstunnel_client_ip()
        except Exception as error:
            logger.error(  # No traceback: it could carry the number.
                "SMSTunnel: a sign-in code was not sent: its text could not be made (%s).", type(error).__name__
            )
            return
        send_code(phone, text, ip=ip, background=sending_in_background())

    def send_unknown_account_sms(self, phone: str, **kwargs: Any) -> None:
        """Sends nothing: a number with no account never gets a text from your phone."""

    def send_account_already_exists_sms(self, phone: str, **kwargs: Any) -> None:
        """Sends nothing, for the same reason."""

    def _smstunnel_client_ip(self) -> str | None:
        request: HttpRequest | None = getattr(self, "request", None)
        if request is None:
            return None
        resolve: Callable[[HttpRequest], str] | None = getattr(self, "get_client_ip", None)
        try:
            candidate = resolve(request) if resolve is not None else None
        except Exception:
            candidate = None  # allauth refuses a request without an IP, or a proxy count that doesn't match.
        return client_ip(request, candidate=candidate)


def _code_length() -> int:
    code_format = _configured_format()
    if not code_format.get("numeric"):
        return DEFAULT_CODE_LENGTH
    return max(_SHORTEST_CODE, min(_LONGEST_CODE, int(code_format.get("length") or 9)))


def _configured_format() -> dict[str, Any]:
    from allauth.account import app_settings

    return dict(getattr(app_settings, "PHONE_VERIFICATION_CODE_FORMAT", None) or {})


def code_format_problem() -> str | None:
    """What is wrong with a configured code format for sending by text, if anything (for the system checks)."""
    code_format = _configured_format()
    if not code_format:
        return None
    if not code_format.get("numeric"):
        return f"it has letters, so a {DEFAULT_CODE_LENGTH}-digit code is used instead"
    length = code_format.get("length")
    if length is not None and not _SHORTEST_CODE <= int(length) <= _LONGEST_CODE:
        return f"its length is {length}, so it is brought within {_SHORTEST_CODE} to {_LONGEST_CODE} digits"
    return None
