from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from django_smstunnel import get_client
from django_smstunnel.conf import DEFAULT_BASE_URL, InvalidSetting, MissingSetting, get_settings

from .support import FakeTransport


def test_the_client_is_made_once(transport: FakeTransport) -> None:
    assert get_client() is get_client()


def test_changing_a_setting_makes_a_new_client(transport: FakeTransport) -> None:
    first = get_client()

    with override_settings(SMSTUNNEL_BASE_URL="https://other.smstunnel.test/"):
        second = get_client()
        second.get_devices()

    assert second is not first
    assert second.base_url == "https://other.smstunnel.test"
    assert transport.last.url == "https://other.smstunnel.test/api/v1/devices"
    assert get_client() is not second, "Leaving override_settings makes a client with the old settings again."


def test_other_settings_keep_the_client(transport: FakeTransport) -> None:
    first = get_client()

    with override_settings(DEBUG=True):
        assert get_client() is first


@override_settings(SMSTUNNEL_BASE_URL=None)
def test_the_default_address_is_smstunnel_io(transport: FakeTransport) -> None:
    assert get_client().base_url == DEFAULT_BASE_URL == "https://smstunnel.io"


@override_settings(SMSTUNNEL_TIMEOUT=5)
def test_the_timeout_reaches_each_request(transport: FakeTransport) -> None:
    get_client().get_devices()

    assert transport.last.timeout == 5.0


def test_the_default_timeout_is_30_seconds(transport: FakeTransport) -> None:
    get_client().get_usage()

    assert transport.last.timeout == 30.0


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_key_is_reported(value: str | None) -> None:
    with override_settings(SMSTUNNEL_API_KEY=value), pytest.raises(MissingSetting):
        get_client()


def test_settings_are_trimmed() -> None:
    with override_settings(
        SMSTUNNEL_API_KEY="  sk_test_unit  ",
        SMSTUNNEL_DEVICE_ID="  phone-1 ",
        SMSTUNNEL_DEFAULT_COUNTRY_CODE=" +40 ",
        SMSTUNNEL_BASE_URL=" https://smstunnel.test/ ",
    ):
        conf = get_settings()

    assert conf.api_key == "sk_test_unit"
    assert conf.device_id == "phone-1"
    assert conf.default_country_code == "+40"
    assert conf.base_url == "https://smstunnel.test"


def test_empty_optional_settings_mean_unset() -> None:
    with override_settings(SMSTUNNEL_DEVICE_ID="", SMSTUNNEL_DEFAULT_COUNTRY_CODE=" "):
        conf = get_settings()

    assert conf.device_id is None
    assert conf.default_country_code is None


def test_the_settings_never_show_the_key() -> None:
    assert "sk_test_unit" not in repr(get_settings())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SMSTUNNEL_API_KEY", 12345),
        ("SMSTUNNEL_BASE_URL", "smstunnel.io"),
        ("SMSTUNNEL_BASE_URL", "ftp://smstunnel.io"),
        ("SMSTUNNEL_TIMEOUT", 0),
        ("SMSTUNNEL_TIMEOUT", "30"),
        ("SMSTUNNEL_TIMEOUT", True),
        ("SMSTUNNEL_DEVICE_ID", 42),
        ("SMSTUNNEL_DEFAULT_COUNTRY_CODE", "RO"),
        ("SMSTUNNEL_DEFAULT_COUNTRY_CODE", "+0"),
        ("SMSTUNNEL_DEFAULT_COUNTRY_CODE", 1.5),
    ],
)
def test_a_wrong_value_is_reported(name: str, value: object) -> None:
    with override_settings(**{name: value}), pytest.raises(InvalidSetting, match=name):
        get_client()


@pytest.mark.parametrize("value", ["+49", "49", "0049", 49, "+1"])
def test_country_codes_in_every_written_form(value: object) -> None:
    with override_settings(SMSTUNNEL_DEFAULT_COUNTRY_CODE=value):
        assert get_settings().default_country_code == str(value)


def test_invalid_settings_are_improperly_configured() -> None:
    assert issubclass(MissingSetting, ImproperlyConfigured)
    assert issubclass(InvalidSetting, ImproperlyConfigured)
