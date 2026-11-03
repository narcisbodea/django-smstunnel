from __future__ import annotations

import pytest
from django.core import checks
from django.core.management import call_command
from django.test import override_settings

from django_smstunnel.checks import check_settings


def ids() -> list[str]:
    return [message.id or "" for message in check_settings()]


def test_a_good_setup_has_nothing_to_report() -> None:
    assert check_settings() == []


def test_the_checks_are_registered() -> None:
    assert check_settings in checks.registry.registry.get_checks()


@override_settings(SMSTUNNEL_API_KEY="")
def test_a_missing_key_is_a_warning() -> None:
    [message] = check_settings()

    assert message.id == "smstunnel.W001"
    assert message.level == checks.WARNING
    assert "dashboard.smstunnel.io/apps" in (message.hint or "")


@override_settings(SMSTUNNEL_API_KEY=42, SMSTUNNEL_TIMEOUT=-1, SMSTUNNEL_DEFAULT_COUNTRY_CODE="RO")
def test_wrong_values_are_errors() -> None:
    messages = check_settings()

    assert [message.id for message in messages] == ["smstunnel.E001"] * 3
    assert all(message.level == checks.ERROR for message in messages)
    assert "SMSTUNNEL_TIMEOUT" in messages[1].msg


@override_settings(SMSTUNNEL_BASE_URL="not a url")
def test_a_wrong_address_is_an_error() -> None:
    assert ids() == ["smstunnel.E001"]


@override_settings(SMSTUNNEL_BASE_URL="http://smstunnel.example.com")
def test_plain_http_to_another_host_is_a_warning() -> None:
    assert ids() == ["smstunnel.W003"]


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:3141", "http://localhost:3141", "http://host.docker.internal:3141", "http://api.localhost"],
)
def test_plain_http_to_a_local_test_server_is_fine(url: str) -> None:
    with override_settings(SMSTUNNEL_BASE_URL=url):
        assert ids() == []


@override_settings(TWO_FACTOR_CALL_GATEWAY="django_smstunnel.two_factor.SmsTunnel")
def test_phone_calls_through_smstunnel_are_a_warning() -> None:
    [message] = check_settings()

    assert message.id == "smstunnel.W002"
    assert "cannot make phone calls" in message.msg


@pytest.mark.parametrize("path", ["two_factor.gateways.fake.Fake", "does.not.Exist", ""])
def test_other_call_providers_are_fine(path: str) -> None:
    with override_settings(TWO_FACTOR_CALL_GATEWAY=path):
        assert "smstunnel.W002" not in ids()


@override_settings(SMSTUNNEL_API_KEY="")
def test_manage_py_check_shows_the_warning(capsys: pytest.CaptureFixture[str]) -> None:
    call_command("check")

    assert "smstunnel.W001" in capsys.readouterr().err
