"""SMSTunnel for Django: send texts from your Django app through your own Android phone, from your own number.

.. code-block:: python

    # settings.py
    INSTALLED_APPS = [..., "django_smstunnel"]
    SMSTUNNEL_API_KEY = os.environ["SMSTUNNEL_API_KEY"]

    # anywhere
    from django_smstunnel import send_sms

    send_sms("+14155550123", "Your order #1042 has shipped.")
"""

from smstunnel import (
    ApiError,
    AuthenticationError,
    DeviceList,
    MessageStatus,
    PermissionDeniedError,
    RateLimitError,
    ScheduledText,
    ScheduledTexts,
    SendResult,
    SmsTunnelError,
    TransportError,
)

from ._version import __version__
from .client import (
    acancel_scheduled,
    alist_scheduled,
    asend_sign_in_code,
    asend_sms,
    cancel_scheduled,
    get_client,
    is_retryable,
    list_scheduled,
    retry_after,
    send_sign_in_code,
    send_sms,
)

__all__ = [
    "ApiError",
    "AuthenticationError",
    "DeviceList",
    "MessageStatus",
    "PermissionDeniedError",
    "RateLimitError",
    "ScheduledText",
    "ScheduledTexts",
    "SendResult",
    "SmsTunnelError",
    "TransportError",
    "__version__",
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
