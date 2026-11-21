"""The django-otp device, through django-otp's real forms."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory, override_settings
from django.utils import timezone
from smstunnel import TransportError

from ..support import FakeTransport, error

pytest.importorskip("django_otp")

from django_otp import devices_for_user, user_has_device
from django_otp.forms import OTPAuthenticationForm, OTPTokenForm

from django_smstunnel.otp.models import CodeNotSentError, SmsTunnelDevice

pytestmark = pytest.mark.django_db

NUMBER = "+14155550123"
PASSWORD = "not-used-elsewhere"


@pytest.fixture
def user() -> Any:
    return get_user_model().objects.create_user("ana", password=PASSWORD)


@pytest.fixture
def device(user: Any) -> Any:
    return SmsTunnelDevice.objects.create(user=user, name="My phone", number=NUMBER)


def sent_code(transport: FakeTransport) -> str:
    match = re.fullmatch(r"Your sign-in code is ([0-9]{6})", transport.last_json()["message"])
    assert match
    return match[1]


def form_data(device: SmsTunnelDevice, **extra: str) -> dict[str, str]:
    return {"username": "ana", "password": PASSWORD, "otp_device": device.persistent_id, **extra}


def test_a_challenge_texts_a_code_through_the_sign_in_route(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    message = device.generate_challenge()

    assert message == "sent by text message"
    assert len(transport.requests) == 1
    assert transport.last.url.endswith("/api/v1/sms/send-2fa")
    assert transport.last_json()["to"] == NUMBER
    device.refresh_from_db()
    assert device.token == sent_code(transport)


def test_the_full_login_form(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    request = RequestFactory().post("/")

    challenge = OTPAuthenticationForm(request, data=form_data(device, otp_challenge="1"))
    assert not challenge.is_valid()
    assert "sent by text message" in str(challenge.errors)

    login = OTPAuthenticationForm(request, data=form_data(device, otp_token=sent_code(transport)))

    assert login.is_valid(), login.errors
    assert login.get_user().otp_device == device


def test_the_token_form_for_a_signed_in_user(user: Any, device: SmsTunnelDevice, transport: FakeTransport) -> None:
    challenge = OTPTokenForm(user, data={"otp_device": device.persistent_id, "otp_challenge": "1"})
    assert not challenge.is_valid()

    form = OTPTokenForm(user, data={"otp_device": device.persistent_id, "otp_token": sent_code(transport)})

    assert form.is_valid(), form.errors


def test_a_wrong_code_fails_and_backs_off(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    device.generate_challenge()
    right = sent_code(transport)
    wrong = "000000" if right != "000000" else "111111"

    assert device.verify_token(wrong) is False
    device.refresh_from_db()
    assert device.throttling_failure_count == 1
    assert device.verify_token(right) is False, "The back-off refuses even the right code at once."


def test_a_code_works_once(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    device.generate_challenge()
    code = sent_code(transport)

    assert device.verify_token(code) is True
    device.refresh_from_db()
    assert device.throttling_failure_count == 0
    assert device.last_used_at is not None
    assert device.verify_token(code) is False


def test_an_expired_code_fails(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    device.generate_challenge()
    code = sent_code(transport)
    SmsTunnelDevice.objects.filter(pk=device.pk).update(valid_until=timezone.now() - timedelta(seconds=1))
    device.refresh_from_db()

    assert device.verify_token(code) is False


def test_the_cooldown_stops_a_second_text(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    device.generate_challenge()
    token = SmsTunnelDevice.objects.get(pk=device.pk).token

    message = device.generate_challenge()

    assert "just sent" in message
    assert len(transport.requests) == 1
    assert SmsTunnelDevice.objects.get(pk=device.pk).token == token


@override_settings(SMSTUNNEL_OTP_COOLDOWN=0)
def test_the_cooldown_can_be_turned_off(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    device.generate_challenge()
    device.generate_challenge()

    assert len(transport.requests) == 2


@override_settings(SMSTUNNEL_OTP_COOLDOWN=0, SMSTUNNEL_SIGNIN_LIMITS={"number": "1/h", "ip": None, "total": None})
def test_the_limit_stops_a_second_text(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    device.generate_challenge()

    with pytest.raises(CodeNotSentError, match="Too many"):
        device.generate_challenge()
    assert len(transport.requests) == 1


@override_settings(SMSTUNNEL_OTP_TOKEN_VALIDITY=30)
def test_the_validity_comes_from_the_setting(device: SmsTunnelDevice, transport: FakeTransport) -> None:
    before = timezone.now()

    device.generate_challenge()

    device.refresh_from_db()
    assert before + timedelta(seconds=25) < device.valid_until < before + timedelta(seconds=35)


@pytest.mark.parametrize(
    "failure",
    [error(401, "Invalid or expired API key"), error(403, "Forbidden"), error(429, "Limit"), error(400, "Bad")],
    ids=["wrong-key", "no-permission", "limit", "refused"],
)
def test_a_refused_code_shows_a_plain_message(device: SmsTunnelDevice, transport: FakeTransport, failure: Any) -> None:
    transport.queue(failure)

    form = OTPAuthenticationForm(RequestFactory().post("/"), data=form_data(device, otp_challenge="1"))

    assert not form.is_valid()
    assert "The code could not be sent" in str(form.errors)
    assert "API key" not in str(form.errors)
    assert NUMBER not in str(form.errors)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("failure", [TransportError("timed out"), error(500, "Crash")], ids=["lost-answer", "5xx"])
def test_a_lost_answer_keeps_the_code_and_sends_nothing_more(
    device: SmsTunnelDevice, transport: FakeTransport, failure: Any
) -> None:
    transport.queue(failure)

    message = device.generate_challenge()

    assert "may have been sent" in message
    assert len(transport.requests) == 1
    code = transport.last_json()["message"].removeprefix("Your sign-in code is ")
    device.refresh_from_db()
    assert device.token == code, "If the text did go out, its code still works."
    assert device.verify_token(code) is True


def test_a_stored_number_without_a_plus_is_refused(user: Any, transport: FakeTransport) -> None:
    device = SmsTunnelDevice.objects.create(user=user, name="Old", number="0721123456")

    with pytest.raises(CodeNotSentError):
        device.generate_challenge()
    assert transport.requests == []


@pytest.mark.parametrize("number", ["0721123456", "14155550123", "+0721123456", "+1234", "+14155550123\n"])
def test_a_form_refuses_a_number_that_is_not_international(user: Any, number: str) -> None:
    device = SmsTunnelDevice(user=user, name="Phone", number=number)

    with pytest.raises(ValidationError) as caught:
        device.full_clean()

    assert "number" in caught.value.message_dict


def test_it_is_a_device_of_the_user(user: Any, device: SmsTunnelDevice) -> None:
    assert user_has_device(user)
    assert list(devices_for_user(user)) == [device]
    assert device.is_interactive()


def test_an_unconfirmed_device_is_not_offered(user: Any) -> None:
    SmsTunnelDevice.objects.create(user=user, name="New", number=NUMBER, confirmed=False)

    assert not user_has_device(user)


def test_the_device_never_shows_its_number_or_code_as_text(device: SmsTunnelDevice) -> None:
    device.token = "482913"

    assert NUMBER not in str(device)
    assert "482913" not in str(device)


def test_the_migration_matches_the_model() -> None:
    call_command("makemigrations", "smstunnel_otp", check=True, dry_run=True, verbosity=0)
