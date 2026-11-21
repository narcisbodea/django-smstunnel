"""The django-phone-verify backend, through its real public endpoints."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from django.conf import settings
from django.test import override_settings
from smstunnel import TransportError

from django_smstunnel.checks import check_signin

from ..support import FakeTransport, error

pytest.importorskip("phone_verify")

from phone_verify.backends import get_sms_backend
from phone_verify.models import SMSVerification
from phone_verify.services import send_security_code_and_generate_session_token
from rest_framework.test import APIClient

from django_smstunnel.phone_verify import SmsTunnelBackend

pytestmark = pytest.mark.django_db

NUMBER = "+14155550123"


def register(client: APIClient, number: str = NUMBER) -> Any:
    return client.post("/api/phone/register", {"phone_number": number}, format="json")


def test_the_backend_is_the_one_configured() -> None:
    assert isinstance(get_sms_backend(NUMBER), SmsTunnelBackend)


def test_a_full_verification(transport: FakeTransport) -> None:
    client = APIClient()

    answer = register(client)

    assert answer.status_code == 200
    assert len(transport.requests) == 1
    assert transport.last.url.endswith("/api/v1/sms/send-2fa")
    body = transport.last_json()
    assert body["to"] == NUMBER
    code = SMSVerification.objects.get(phone_number=NUMBER).security_code
    assert body["message"] == f"Your Example code is {code}"

    verified = client.post(
        "/api/phone/verify",
        {"phone_number": NUMBER, "session_token": answer.data["session_token"], "security_code": code},
        format="json",
    )
    assert verified.status_code == 200


def test_a_wrong_code_is_refused(transport: FakeTransport) -> None:
    client = APIClient()
    answer = register(client)
    code = SMSVerification.objects.get(phone_number=NUMBER).security_code
    wrong = "000000" if code != "000000" else "111111"

    refused = client.post(
        "/api/phone/verify",
        {"phone_number": NUMBER, "session_token": answer.data["session_token"], "security_code": wrong},
        format="json",
    )

    assert refused.status_code == 400


@pytest.mark.parametrize(
    "failure",
    [error(401, "Invalid or expired API key"), error(429, "Limit"), error(500, "Crash"), TransportError("timed out")],
    ids=["wrong-key", "limit", "server-error", "lost-answer"],
)
def test_a_failed_send_keeps_the_same_answer_and_the_number_out_of_the_logs(
    transport: FakeTransport, failure: Any, caplog: pytest.LogCaptureFixture
) -> None:
    transport.queue(failure)

    with caplog.at_level(logging.DEBUG):
        answer = register(APIClient())

    assert answer.status_code == 200
    assert "session_token" in answer.data
    assert len(transport.requests) == 1
    assert caplog.records
    assert NUMBER not in caplog.text
    assert "4155550123" not in caplog.text


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "2/h", "ip": None, "total": None})
def test_the_limit_per_number(transport: FakeTransport) -> None:
    answers = [register(APIClient()).status_code for _ in range(4)]

    assert answers == [200, 200, 200, 200]
    assert len(transport.requests) == 2


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "2/h", "total": None})
def test_the_public_endpoint_is_limited_per_ip(transport: FakeTransport) -> None:
    client = APIClient(REMOTE_ADDR="203.0.113.7")

    for last_digit in range(5):
        assert register(client, f"+1415555012{last_digit}").status_code == 200

    assert len(transport.requests) == 2


def test_a_number_that_is_not_international_gets_no_text(transport: FakeTransport) -> None:
    SmsTunnelBackend().send_sms("0721123456", "Your code is 482913")

    assert transport.requests == []


def test_a_phone_number_object_is_sent_in_international_form(transport: FakeTransport) -> None:
    from phonenumber_field.phonenumber import PhoneNumber

    SmsTunnelBackend().send_sms(PhoneNumber.from_string(NUMBER), "Your code is 482913")

    assert transport.last_json()["to"] == NUMBER


def test_it_never_sends_to_a_list(transport: FakeTransport) -> None:
    with pytest.raises(NotImplementedError, match="one text per number"):
        SmsTunnelBackend().send_bulk_sms([NUMBER, "+14155550124"], "Your code is 482913")

    assert transport.requests == []


def test_the_service_keeps_its_session_token_when_the_send_fails(transport: FakeTransport) -> None:
    transport.queue(error(401, "Invalid or expired API key"))

    assert send_security_code_and_generate_session_token(NUMBER)


def test_a_correct_setup_has_nothing_to_report() -> None:
    assert check_signin() == []


def test_without_the_middleware_the_check_says_so() -> None:
    middleware = [name for name in settings.MIDDLEWARE if "ClientIP" not in name]

    with override_settings(MIDDLEWARE=middleware):
        assert "smstunnel.W007" in [message.id for message in check_signin()]


@pytest.mark.parametrize("length", [3, 11])
def test_a_code_length_it_cant_send_is_an_error(length: int) -> None:
    config = {**getattr(settings, "PHONE_VERIFICATION", {}), "TOKEN_LENGTH": length}

    with override_settings(PHONE_VERIFICATION=config):
        assert "smstunnel.E004" in [message.id for message in check_signin()]
