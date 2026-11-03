from __future__ import annotations

import asyncio
import uuid

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

import django_smstunnel
from django_smstunnel import (
    ApiError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    SmsTunnelError,
    TransportError,
    asend_sign_in_code,
    asend_sms,
    is_retryable,
    retry_after,
    send_sign_in_code,
    send_sms,
)

from .support import MESSAGE_ID, FakeTransport, error, sent


def test_send_sms_posts_one_text_with_the_api_key(transport: FakeTransport) -> None:
    result = send_sms("+14155550123", "Your order #1042 has shipped.")

    request = transport.last
    assert request.method == "POST"
    assert request.url == "https://smstunnel.test/api/v1/sms/send"
    assert request.headers["X-API-Key"] == "sk_test_unit"
    assert request.headers["Content-Type"] == "application/json"
    assert "Idempotency-Key" not in request.headers
    assert transport.last_json() == {"to": "+14155550123", "message": "Your order #1042 has shipped."}
    assert result.message_id == MESSAGE_ID
    assert result.queued is False
    assert result.remaining == {"clean": 99, "total": "unlimited"}


def test_the_user_agent_names_this_package(transport: FakeTransport) -> None:
    send_sms("+14155550123", "Hello")

    user_agent = transport.last.headers["User-Agent"]
    assert user_agent.startswith("smstunnel-python/")
    assert f"django-smstunnel/{django_smstunnel.__version__} Django/" in user_agent


def test_an_offline_phone_queues_the_text(transport: FakeTransport) -> None:
    transport.queue(sent(queued=True))

    result = send_sms("+14155550123", "See you at 10:30.")

    assert result.queued is True


@override_settings(SMSTUNNEL_DEVICE_ID="phone-1", SMSTUNNEL_DEFAULT_COUNTRY_CODE="+40")
def test_settings_pick_the_phone_and_the_country_code(transport: FakeTransport) -> None:
    send_sms("0721123456", "Hello")

    assert transport.last_json() == {
        "to": "0721123456",
        "message": "Hello",
        "deviceId": "phone-1",
        "defaultCountryCode": "+40",
    }


@override_settings(SMSTUNNEL_DEVICE_ID="phone-1", SMSTUNNEL_DEFAULT_COUNTRY_CODE=40)
def test_the_call_wins_over_the_settings(transport: FakeTransport) -> None:
    send_sms("4155550123", "Hello", device_id="phone-2", default_country_code="+1")

    body = transport.last_json()
    assert body["deviceId"] == "phone-2"
    assert body["defaultCountryCode"] == "+1"


@override_settings(SMSTUNNEL_DEFAULT_COUNTRY_CODE=40)
def test_a_numeric_country_code_setting_is_sent_as_a_string(transport: FakeTransport) -> None:
    send_sms("0721123456", "Hello")

    assert transport.last_json()["defaultCountryCode"] == "40"


def test_an_idempotency_key_is_sent_as_a_header(transport: FakeTransport) -> None:
    send_sms("+14155550123", "Your order #1042 has shipped.", idempotency_key="order-1042-shipped")

    assert transport.last.headers["Idempotency-Key"] == "order-1042-shipped"


def test_a_uuid_is_a_valid_idempotency_key(transport: FakeTransport) -> None:
    key = uuid.uuid4()

    send_sms("+14155550123", "Hello", idempotency_key=key)

    assert transport.last.headers["Idempotency-Key"] == str(key)


def test_the_key_belongs_to_one_send_only(transport: FakeTransport) -> None:
    send_sms("+14155550123", "First", idempotency_key="key-1")
    send_sms("+14155550123", "Second")
    django_smstunnel.get_client().get_devices()

    assert transport.requests[0].headers["Idempotency-Key"] == "key-1"
    assert "Idempotency-Key" not in transport.requests[1].headers
    assert "Idempotency-Key" not in transport.requests[2].headers


def test_the_key_is_cleared_when_the_send_fails(transport: FakeTransport) -> None:
    transport.queue(error(400, "Phone number must contain 7-25 characters"))

    with pytest.raises(ApiError):
        send_sms("12", "Hello", idempotency_key="key-1")
    send_sms("+14155550123", "Hello")

    assert "Idempotency-Key" not in transport.last.headers


@pytest.mark.parametrize("key", ["", "   ", "x" * 256, "naïve", "tab\there"])
def test_an_unusable_idempotency_key_is_refused_before_sending(transport: FakeTransport, key: str) -> None:
    with pytest.raises(ValueError, match="idempotency_key must be 1 to 255 printable ASCII characters"):
        send_sms("+14155550123", "Hello", idempotency_key=key)

    assert transport.requests == []


def test_an_idempotency_key_must_be_text(transport: FakeTransport) -> None:
    with pytest.raises(TypeError):
        send_sms("+14155550123", "Hello", idempotency_key=1042)  # type: ignore[arg-type]


def test_an_empty_number_or_text_is_refused_before_sending(transport: FakeTransport) -> None:
    with pytest.raises(ValueError, match="recipient"):
        send_sms("", "Hello")
    with pytest.raises(ValueError, match="text"):
        send_sms("+14155550123", "  ")

    assert transport.requests == []


def test_send_sign_in_code_uses_the_sign_in_code_route(transport: FakeTransport) -> None:
    result = send_sign_in_code("+14155550123", "Your sign-in code is 482913", idempotency_key="login-7")

    assert transport.last.url == "https://smstunnel.test/api/v1/sms/send-2fa"
    assert transport.last.headers["Idempotency-Key"] == "login-7"
    assert transport.last_json() == {"to": "+14155550123", "message": "Your sign-in code is 482913"}
    assert result.message_id == MESSAGE_ID


@override_settings(SMSTUNNEL_DEVICE_ID="phone-1")
def test_send_sign_in_code_uses_the_settings_too(transport: FakeTransport) -> None:
    send_sign_in_code("+14155550123", "Your sign-in code is 482913")

    assert transport.last_json()["deviceId"] == "phone-1"


def test_a_text_without_a_code_is_refused_by_smstunnel(transport: FakeTransport) -> None:
    transport.queue(error(400, "No valid verification code found"))

    with pytest.raises(ApiError) as caught:
        send_sign_in_code("+14155550123", "There is no code here")

    assert caught.value.status_code == 400
    assert caught.value.message == "No valid verification code found"


def test_a_wrong_key_raises_authentication_error(transport: FakeTransport) -> None:
    transport.queue(error(401, "Invalid or expired API key"))

    with pytest.raises(AuthenticationError) as caught:
        send_sms("+14155550123", "Hello")

    assert caught.value.status_code == 401
    assert str(caught.value) == "Invalid or expired API key"
    assert not is_retryable(caught.value)


def test_a_missing_permission_raises_permission_denied_with_the_code(transport: FakeTransport) -> None:
    transport.queue(
        error(403, "This API key cannot send texts", code="PERMISSION_REQUIRED"),
    )

    with pytest.raises(PermissionDeniedError) as caught:
        send_sms("+14155550123", "Hello")

    assert caught.value.error_code == "PERMISSION_REQUIRED"
    assert not is_retryable(caught.value)


def test_too_many_requests_carries_retry_after(transport: FakeTransport) -> None:
    transport.queue(error(429, "ThrottlerException: Too Many Requests", headers={"retry-after": "17"}))

    with pytest.raises(RateLimitError) as caught:
        send_sms("+14155550123", "Hello", idempotency_key="k")

    assert caught.value.retry_after == 17.0
    assert retry_after(caught.value) == 17.0
    assert is_retryable(caught.value)
    assert len(transport.requests) == 1, "A send is never retried automatically."


def test_a_server_error_is_retryable_but_not_retried(transport: FakeTransport) -> None:
    transport.queue(error(503, "Service Unavailable"))

    with pytest.raises(ApiError) as caught:
        send_sms("+14155550123", "Hello", idempotency_key="k")

    assert caught.value.status_code == 503
    assert is_retryable(caught.value)
    assert len(transport.requests) == 1


def test_a_send_still_running_with_the_same_key_is_retryable(transport: FakeTransport) -> None:
    transport.queue(
        error(409, "still being processed", code="IDEMPOTENCY_KEY_IN_PROGRESS", headers={"retry-after": "1"}),
        error(422, "already used for a different request", code="IDEMPOTENCY_KEY_REUSED"),
    )

    with pytest.raises(ApiError) as in_progress:
        send_sms("+14155550123", "Hello", idempotency_key="k")
    with pytest.raises(ApiError) as reused:
        send_sms("+14155550123", "Another text", idempotency_key="k")

    assert is_retryable(in_progress.value)
    assert retry_after(in_progress.value) == 1.0
    assert reused.value.error_code == "IDEMPOTENCY_KEY_REUSED"
    assert not is_retryable(reused.value)
    assert retry_after(reused.value) is None


def test_no_answer_raises_transport_error(transport: FakeTransport) -> None:
    transport.queue(TransportError("Could not reach SMSTunnel at https://smstunnel.test: timed out"))

    with pytest.raises(SmsTunnelError) as caught:
        send_sms("+14155550123", "Hello", idempotency_key="k")

    assert isinstance(caught.value, TransportError)
    assert is_retryable(caught.value)
    assert retry_after(caught.value) is None


def test_other_exceptions_are_not_retryable() -> None:
    assert not is_retryable(ValueError("no"))
    assert retry_after(ValueError("no")) is None


@override_settings(SMSTUNNEL_API_KEY="")
def test_a_missing_key_says_where_to_get_one(transport: FakeTransport) -> None:
    with pytest.raises(ImproperlyConfigured, match=r"SMSTUNNEL_API_KEY is not set.*Apps → Add an app"):
        send_sms("+14155550123", "Hello")

    assert transport.requests == []


def test_asend_sms_sends_from_async_code(transport: FakeTransport) -> None:
    result = asyncio.run(asend_sms("+14155550123", "Your table is ready.", idempotency_key="table-9"))

    assert result.message_id == MESSAGE_ID
    assert transport.last.url.endswith("/api/v1/sms/send")
    assert transport.last.headers["Idempotency-Key"] == "table-9"


def test_asend_sign_in_code_sends_from_async_code(transport: FakeTransport) -> None:
    asyncio.run(asend_sign_in_code("+14155550123", "Your code is 1234"))

    assert transport.last.url.endswith("/api/v1/sms/send-2fa")
    assert "Idempotency-Key" not in transport.last.headers
