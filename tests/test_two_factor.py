from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.template import TemplateDoesNotExist
from django.test import override_settings
from django.utils import translation

from django_smstunnel import ApiError
from django_smstunnel.two_factor import SmsTunnel

from .support import FakeTransport, error


class PhoneNumber:
    """Like phonenumber_field's PhoneNumber, as far as the class reads it."""

    def __init__(self, e164: str) -> None:
        self.as_e164 = e164

    def __str__(self) -> str:
        return "(0721) 123 456"


def test_send_sms_texts_the_code_to_the_device_number(transport: FakeTransport) -> None:
    SmsTunnel().send_sms(device=SimpleNamespace(number=PhoneNumber("+14155550123")), token="482913")

    assert transport.last.url == "https://smstunnel.test/api/v1/sms/send-2fa"
    assert transport.last_json() == {"to": "+14155550123", "message": "Your OTP token is 482913"}
    assert "Idempotency-Key" not in transport.last.headers


def test_a_plain_number_works_too(transport: FakeTransport) -> None:
    SmsTunnel().send_sms(device=SimpleNamespace(number="+14155550124"), token="1234")

    assert transport.last_json()["to"] == "+14155550124"


@override_settings(SMSTUNNEL_DEVICE_ID="phone-1", SMSTUNNEL_DEFAULT_COUNTRY_CODE="+40")
def test_the_settings_pick_the_phone_and_complete_local_numbers(transport: FakeTransport) -> None:
    SmsTunnel().send_sms(device=SimpleNamespace(number="0721123456"), token="482913")

    body = transport.last_json()
    assert body["deviceId"] == "phone-1"
    assert body["defaultCountryCode"] == "+40"


def test_the_template_can_be_overridden(transport: FakeTransport, tmp_path: Path) -> None:
    folder = tmp_path / "django_smstunnel" / "two_factor"
    folder.mkdir(parents=True)
    (folder / "sms_message.txt").write_text(
        "{{ token }} is your Example & Co sign-in code.\n\n@example.com #{{ token }}\n", encoding="utf-8"
    )
    templates = [{**settings.TEMPLATES[0], "DIRS": [str(tmp_path)]}]

    with override_settings(TEMPLATES=templates):
        SmsTunnel().send_sms(device=SimpleNamespace(number="+14155550123"), token="482913")

    assert transport.last_json()["message"] == "482913 is your Example & Co sign-in code.\n\n@example.com #482913"


def test_a_subclass_can_use_its_own_template() -> None:
    class Custom(SmsTunnel):
        template_name = "custom/does-not-exist.txt"

    with pytest.raises(TemplateDoesNotExist):
        Custom().render_message("482913")


def test_without_the_template_the_same_words_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> str:
        raise TemplateDoesNotExist("django_smstunnel/two_factor/sms_message.txt")

    monkeypatch.setattr("django_smstunnel.two_factor.render_to_string", missing)

    assert SmsTunnel().render_message("482913") == "Your OTP token is 482913"


def test_a_subclass_can_add_an_idempotency_key(transport: FakeTransport) -> None:
    class Keyed(SmsTunnel):
        def idempotency_key(self, device: object, token: str) -> str:
            return f"sign-in-{token}"

    Keyed().send_sms(device=SimpleNamespace(number="+14155550123"), token="482913")

    assert transport.last.headers["Idempotency-Key"] == "sign-in-482913"


def test_phone_calls_are_not_supported() -> None:
    with pytest.raises(NotImplementedError, match="cannot make phone calls"):
        SmsTunnel().make_call(device=SimpleNamespace(number="+14155550123"), token="482913")


def test_a_refused_code_reaches_the_caller(transport: FakeTransport) -> None:
    transport.queue(error(400, "No valid verification code found"))

    with pytest.raises(ApiError):
        SmsTunnel().send_sms(device=SimpleNamespace(number="+14155550123"), token="12")


# With django-two-factor-auth itself.

two_factor_installed = pytest.mark.skipif(
    not getattr(settings, "HAS_TWO_FACTOR", False), reason="django-two-factor-auth is not installed"
)


@two_factor_installed
def test_the_code_is_translated_with_django_two_factor_auths_catalog(transport: FakeTransport) -> None:
    with translation.override("de"):
        SmsTunnel().send_sms(device=SimpleNamespace(number="+14155550123"), token="482913")

    assert transport.last_json()["message"] == "Ihr Einmal-Token ist 482913"


@two_factor_installed
def test_two_factors_send_sms_uses_the_setting(transport: FakeTransport) -> None:
    from two_factor.gateways import send_sms

    send_sms(device=SimpleNamespace(number=PhoneNumber("+14155550125")), token="777123")

    assert transport.last_json() == {"to": "+14155550125", "message": "Your OTP token is 777123"}


@two_factor_installed
@pytest.mark.django_db
def test_a_phone_device_challenge_sends_its_current_code(transport: FakeTransport) -> None:
    from django.contrib.auth import get_user_model
    from two_factor.plugins.phonenumber.models import PhoneDevice
    from two_factor.plugins.registry import registry

    user = get_user_model().objects.create_user("ana", password="not-used")
    device = PhoneDevice.objects.create(user=user, name="default", number="+14155550126", method="sms")

    device.generate_challenge()

    body = transport.last_json()
    assert transport.last.url.endswith("/api/v1/sms/send-2fa")
    assert body["to"] == "+14155550126"
    code = body["message"].removeprefix("Your OTP token is ")
    assert code.isdigit()
    assert len(code) == 6
    assert device.verify_token(code)
    assert registry.get_method("sms") is not None, "django-two-factor-auth offers text messages."
