"""django-allauth's real phone sign-in, against a fake transport."""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.checks import Error, Warning
from django.test import Client, override_settings
from django.urls import reverse
from smstunnel import TransportError

from django_smstunnel import signin
from django_smstunnel.checks import check_signin

from ..support import FakeTransport, error

pytest.importorskip("allauth")

from allauth.account.adapter import DefaultAccountAdapter, get_adapter

from django_smstunnel.allauth import SmsTunnelAccountAdapterMixin

from .adapters import PhoneAdapter
from .phoneapp.models import UserPhone

pytestmark = pytest.mark.django_db

KNOWN = "+14155550123"
UNKNOWN = "+14155550999"


@pytest.fixture
def member() -> Any:
    user = get_user_model().objects.create_user("ana", password="not-used-here")
    UserPhone.objects.create(user=user, number=KNOWN, verified=True)
    return user


def code_from(transport: FakeTransport) -> str:
    match = re.fullmatch(r"Your sign-in code is ([0-9]{6})", transport.last_json()["message"])
    assert match, "The text holds the code as digits."
    return match[1]


def request_code(client: Client, number: str) -> Any:
    return client.post(reverse("account_request_login_code"), {"phone": number})


def visible(response: Any) -> tuple[int, str, list[str]]:
    """What a visitor can compare between two answers, with the number taken out."""
    messages = [str(message).replace(KNOWN, "N").replace(UNKNOWN, "N") for message in response.wsgi_request._messages]
    return response.status_code, response.headers.get("Location", ""), messages


def test_a_full_phone_sign_in(client: Client, member: Any, transport: FakeTransport) -> None:
    answer = request_code(client, KNOWN)

    assert answer.status_code == 302
    assert answer.headers["Location"] == reverse("account_confirm_login_code")
    assert len(transport.requests) == 1
    assert transport.last.url.endswith("/api/v1/sms/send-2fa")
    assert transport.last_json()["to"] == KNOWN
    assert "Idempotency-Key" in transport.last.headers

    answer = client.post(reverse("account_confirm_login_code"), {"code": code_from(transport)}, follow=True)

    assert answer.redirect_chain[-1][0] == settings.LOGIN_REDIRECT_URL
    assert answer.content == b"signed in: True"
    assert len(transport.requests) == 1


def test_a_wrong_code_does_not_sign_in(client: Client, member: Any, transport: FakeTransport) -> None:
    request_code(client, KNOWN)
    right = code_from(transport)
    wrong = "000000" if right != "000000" else "111111"

    answer = client.post(reverse("account_confirm_login_code"), {"code": wrong})

    assert answer.status_code == 200
    assert not answer.wsgi_request.user.is_authenticated


def test_an_unknown_number_gets_no_text(client: Client, member: Any, transport: FakeTransport) -> None:
    answer = request_code(client, UNKNOWN)

    assert answer.status_code == 302
    assert answer.headers["Location"] == reverse("account_confirm_login_code")
    assert transport.requests == []


def test_an_unknown_number_can_never_sign_in(client: Client, member: Any, transport: FakeTransport) -> None:
    request_code(client, UNKNOWN)

    for guess in ("000000", "123456", ""):
        answer = client.post(reverse("account_confirm_login_code"), {"code": guess})
        assert not answer.wsgi_request.user.is_authenticated
    assert transport.requests == []


@pytest.mark.parametrize(
    "failure",
    [
        error(401, "Invalid or expired API key"),
        error(403, "Forbidden", code="PERMISSION_REQUIRED"),
        error(429, "Daily limit", code="KEY_DAILY_CAP_REACHED"),
        error(500, "Crash"),
        TransportError("timed out"),
    ],
    ids=["wrong-key", "no-permission", "daily-cap", "server-error", "lost-answer"],
)
def test_a_failed_send_looks_like_an_unknown_number(
    member: Any, transport: FakeTransport, failure: Any, caplog: pytest.LogCaptureFixture
) -> None:
    transport.queue(failure)

    with caplog.at_level(logging.ERROR, logger="django_smstunnel.signin"):
        known = request_code(Client(), KNOWN)
    unknown = request_code(Client(), UNKNOWN)

    assert visible(known) == visible(unknown)
    assert len(transport.requests) == 1, "Never retried."
    assert caplog.records, "The owner finds it in the log."
    assert KNOWN not in caplog.text


def test_a_good_send_also_looks_like_an_unknown_number(member: Any, transport: FakeTransport) -> None:
    known = request_code(Client(), KNOWN)
    unknown = request_code(Client(), UNKNOWN)

    assert visible(known) == visible(unknown)


@override_settings(SMSTUNNEL_SIGNIN_BACKGROUND=True)
def test_the_answer_does_not_wait_for_the_send(
    client: Client, member: Any, transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    real = transport.send

    def blocked(request: Any) -> Any:
        release.wait(10)
        return real(request)

    monkeypatch.setattr(transport, "send", blocked)

    answer = request_code(client, KNOWN)

    assert answer.status_code == 302
    assert transport.requests == [], "The page must answer before the text goes out."
    release.set()
    assert signin._pool.drain(10)
    assert len(transport.requests) == 1


@pytest.mark.parametrize("number", ["14155550123", "0721123456", "+0721123456"])
def test_a_number_without_a_plus_is_refused(client: Client, member: Any, transport: FakeTransport, number: str) -> None:
    answer = request_code(client, number)

    assert answer.status_code == 200
    assert answer.context["form"].errors
    assert transport.requests == []


def test_a_stored_number_without_a_plus_gets_no_text(transport: FakeTransport) -> None:
    get_adapter().send_verification_code_sms(user=None, phone="0721123456", code="482913")

    assert transport.requests == []


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "2/h", "ip": None, "total": None})
def test_the_number_limit_applies_and_the_page_does_not_change(member: Any, transport: FakeTransport) -> None:
    answers = [visible(request_code(Client(), KNOWN)) for _ in range(4)]

    assert len(set(map(str, answers))) == 1
    assert len(transport.requests) == 2


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "1/h", "total": None})
def test_the_ip_limit_applies(member: Any, transport: FakeTransport) -> None:
    Client(REMOTE_ADDR="203.0.113.7").post(reverse("account_request_login_code"), {"phone": KNOWN})
    Client(REMOTE_ADDR="203.0.113.7").post(reverse("account_request_login_code"), {"phone": KNOWN})
    assert len(transport.requests) == 1

    Client(REMOTE_ADDR="203.0.113.8").post(reverse("account_request_login_code"), {"phone": KNOWN})
    assert len(transport.requests) == 2


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}})
def test_a_cache_that_keeps_nothing_sends_nothing_and_the_page_does_not_change(
    member: Any, transport: FakeTransport
) -> None:
    known = request_code(Client(), KNOWN)
    unknown = request_code(Client(), UNKNOWN)

    assert visible(known) == visible(unknown)
    assert transport.requests == []


def test_codes_are_six_digits_by_default() -> None:
    code = get_adapter().generate_phone_verification_code(user=None, phone=KNOWN)

    assert re.fullmatch(r"[0-9]{6}", code)


@pytest.mark.parametrize(
    ("configured", "length"),
    [
        ({"numeric": True, "length": 8, "dashed": True}, 8),
        ({"numeric": True}, 9),
        ({"numeric": True, "length": 4}, 4),
        ({"numeric": True, "length": 20}, 10),
        ({"numeric": True, "length": 2}, 4),
        ({"numeric": False, "length": 8}, 6),
    ],
)
def test_a_configured_format_stays_sendable(configured: dict[str, Any], length: int) -> None:
    with override_settings(ACCOUNT_PHONE_VERIFICATION_CODE_FORMAT=configured):
        code = get_adapter().generate_phone_verification_code(user=None, phone=KNOWN)

    assert re.fullmatch(rf"[0-9]{{{length}}}", code)


@pytest.mark.parametrize(("code", "digits"), [("123-456", "123456"), ("123-456-789", "123456789"), ("12 34", "1234")])
def test_a_dashed_code_is_sent_as_plain_digits(transport: FakeTransport, code: str, digits: str) -> None:
    get_adapter().send_verification_code_sms(user=None, phone=KNOWN, code=code)

    assert transport.last_json()["message"] == f"Your sign-in code is {digits}"


def test_a_broken_template_does_not_show_on_the_page(
    member: Any, transport: FakeTransport, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> str:
        raise ValueError(f"template failed for {KNOWN}")

    monkeypatch.setattr("django_smstunnel.allauth.code_text", broken)

    known = request_code(Client(), KNOWN)
    unknown = request_code(Client(), UNKNOWN)

    assert visible(known) == visible(unknown)
    assert transport.requests == []
    assert KNOWN not in caplog.text


def test_the_two_enumeration_texts_send_nothing(transport: FakeTransport) -> None:
    adapter = get_adapter()

    adapter.send_unknown_account_sms(UNKNOWN)
    adapter.send_account_already_exists_sms(KNOWN)

    assert transport.requests == []


def test_allauth_sees_a_complete_phone_implementation() -> None:
    assert get_adapter()._has_phone_impl


def test_sign_up_with_a_phone_sends_a_code_and_verifies_it(client: Client, transport: FakeTransport) -> None:
    answer = client.post(
        reverse("account_signup"),
        {"phone": KNOWN, "username": "newuser", "password1": "Strong-pass-731", "password2": "Strong-pass-731"},
    )
    assert answer.status_code == 302
    assert answer.headers["Location"] == reverse("account_verify_phone")
    assert transport.last_json()["to"] == KNOWN

    answer = client.post(reverse("account_verify_phone"), {"code": code_from(transport)}, follow=True)

    assert answer.content == b"signed in: True"
    assert UserPhone.objects.get(number=KNOWN).verified is True


def test_signing_up_with_a_taken_number_texts_nothing_and_says_nothing(
    client: Client, member: Any, transport: FakeTransport
) -> None:
    answer = client.post(
        reverse("account_signup"),
        {"phone": KNOWN, "username": "other", "password1": "Strong-pass-731", "password2": "Strong-pass-731"},
    )

    assert answer.status_code == 302
    assert answer.headers["Location"] == reverse("account_verify_phone")
    assert transport.requests == []


# System checks


def test_a_correct_setup_has_nothing_to_report() -> None:
    assert check_signin() == []


class Reversed(DefaultAccountAdapter, SmsTunnelAccountAdapterMixin):  # type: ignore[misc,unused-ignore]
    pass


def test_the_mixin_in_the_wrong_place_is_an_error() -> None:
    with override_settings(ACCOUNT_ADAPTER=f"{__name__}.Reversed"):
        messages = check_signin()

    assert [message.id for message in messages] == ["smstunnel.E003"]
    assert isinstance(messages[0], Error)


@override_settings(ACCOUNT_PREVENT_ENUMERATION=False)
def test_turning_enumeration_prevention_off_is_a_warning() -> None:
    messages = check_signin()

    assert [message.id for message in messages] == ["smstunnel.W004"]
    assert isinstance(messages[0], Warning)


@pytest.mark.parametrize("configured", [{"numeric": False}, {"numeric": True, "length": 12}])
def test_a_format_that_cant_be_texted_is_a_warning(configured: dict[str, Any]) -> None:
    with override_settings(ACCOUNT_PHONE_VERIFICATION_CODE_FORMAT=configured):
        assert [message.id for message in check_signin()] == ["smstunnel.W005"]


def test_the_adapter_in_use_is_the_test_one() -> None:
    assert isinstance(get_adapter(), PhoneAdapter)
