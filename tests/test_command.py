from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from .support import FakeTransport, answer, error, sent

PHONES = answer(
    200,
    {
        "success": True,
        "data": {
            "devices": [
                {"id": "phone-1", "name": "Pixel 8", "isOnline": True, "lastSeenAt": "2026-11-03T10:00:00.000Z"},
                {"id": "phone-2", "name": "Old Galaxy", "isOnline": False, "lastSeenAt": None},
            ],
            "online": 1,
            "total": 2,
        },
    },
)
NO_PHONES = answer(200, {"success": True, "data": {"devices": [], "online": 0, "total": 0}})


def run(*args: str) -> str:
    out = StringIO()
    call_command("smstunnel_test", *args, stdout=out, no_color=True)
    return out.getvalue()


def test_lists_the_phones_and_sends_nothing(transport: FakeTransport) -> None:
    transport.queue(PHONES)

    output = run()

    assert "The API key works (https://smstunnel.test)." in output
    assert "Pixel 8 (phone-1): online" in output
    assert "Old Galaxy (phone-2): offline" in output
    assert [request.url for request in transport.requests] == ["https://smstunnel.test/api/v1/devices"]


def test_says_when_no_phone_is_linked(transport: FakeTransport) -> None:
    transport.queue(NO_PHONES)

    assert "No phone is linked to this account yet" in run()


@override_settings(SMSTUNNEL_DEVICE_ID="phone-9")
def test_says_when_the_device_setting_is_not_a_phone_of_the_account(transport: FakeTransport) -> None:
    transport.queue(PHONES)

    assert "SMSTUNNEL_DEVICE_ID 'phone-9' is not one of this account's phones." in run()


def test_sends_one_test_text_with_to(transport: FakeTransport) -> None:
    transport.queue(PHONES, sent("6740f1c2a8b9c0d1e2f3a4b6", queued=True))

    output = run("--to", "+14155550123", "--message", "Hello from the test")

    assert transport.last.url == "https://smstunnel.test/api/v1/sms/send"
    assert transport.last_json() == {"to": "+14155550123", "message": "Hello from the test"}
    assert "Test text accepted, message id 6740f1c2a8b9c0d1e2f3a4b6." in output
    assert "The phone is offline" in output


def test_a_wrong_key_is_a_command_error(transport: FakeTransport) -> None:
    transport.queue(error(401, "Invalid or expired API key"))

    with pytest.raises(CommandError, match="Invalid or expired API key"):
        run()


@override_settings(SMSTUNNEL_API_KEY="")
def test_a_missing_key_is_a_command_error(transport: FakeTransport) -> None:
    with pytest.raises(CommandError, match="SMSTUNNEL_API_KEY is not set"):
        run()


def test_a_refused_test_text_is_a_command_error(transport: FakeTransport) -> None:
    transport.queue(PHONES, error(400, "Phone number must contain 7-25 characters"))

    with pytest.raises(CommandError, match="The test text was not sent: Phone number must contain"):
        run("--to", "12")
