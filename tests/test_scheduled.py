"""Scheduled texts: ``send_sms(..., send_at=...)``, ``list_scheduled()`` and ``cancel_scheduled()``."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import pytest
from django.test import override_settings
from django.utils import timezone

from django_smstunnel import (
    ApiError,
    PermissionDeniedError,
    ScheduledText,
    ScheduledTexts,
    SendResult,
    acancel_scheduled,
    alist_scheduled,
    asend_sms,
    cancel_scheduled,
    get_client,
    is_retryable,
    list_scheduled,
    send_sms,
)

from .support import SCHEDULED_ID, FakeTransport, answer, error, scheduled, scheduled_text, sent

TO = "+14155550123"
TEXT = "Reminder: your appointment is tomorrow at 10:30."
UTC = dt_timezone.utc


def test_an_aware_datetime_schedules_the_text(transport: FakeTransport) -> None:
    transport.queue(scheduled())
    when = datetime(2026, 11, 16, 9, 0, tzinfo=dt_timezone(timedelta(hours=2)))

    text = send_sms(TO, TEXT, send_at=when)

    request = transport.last
    assert request.method == "POST"
    assert request.url == "https://smstunnel.test/api/v1/sms/send"
    assert transport.last_json() == {"to": TO, "message": TEXT, "sendAt": "2026-11-16T09:00:00+02:00"}
    assert isinstance(text, ScheduledText)
    assert text.id == SCHEDULED_ID
    assert text.status == ScheduledText.SCHEDULED
    assert text.send_at == datetime(2026, 11, 16, 7, 0, tzinfo=UTC)


def test_timezone_now_plus_a_delay_is_aware(transport: FakeTransport) -> None:
    transport.queue(scheduled())
    when = timezone.now() + timedelta(hours=1)

    send_sms(TO, TEXT, send_at=when)

    sent_at = transport.last_json()["sendAt"]
    assert sent_at.endswith("+00:00")
    assert datetime.fromisoformat(sent_at) == when.replace(microsecond=0)


def test_with_use_tz_a_naive_datetime_is_refused_before_anything_is_sent(transport: FakeTransport) -> None:
    with pytest.raises(ValueError, match="naive datetime, but USE_TZ is True") as caught:
        send_sms(TO, TEXT, send_at=datetime(2026, 11, 16, 9, 0))

    assert "timezone.make_aware" in str(caught.value)
    assert transport.requests == []


@override_settings(USE_TZ=False, TIME_ZONE="Europe/Bucharest")
def test_without_use_tz_a_naive_datetime_is_local_time(transport: FakeTransport) -> None:
    transport.queue(scheduled(), scheduled())

    send_sms(TO, TEXT, send_at=datetime(2026, 11, 16, 9, 0))
    assert transport.last_json()["sendAt"] == "2026-11-16T09:00:00+02:00"

    # The current time zone, as Django uses it, when the request activated another one.
    with timezone.override("America/New_York"):
        send_sms(TO, TEXT, send_at=datetime(2026, 11, 16, 9, 0))
    assert transport.last_json()["sendAt"] == "2026-11-16T09:00:00-05:00"


@override_settings(USE_TZ=False)
def test_without_use_tz_an_aware_datetime_keeps_its_offset(transport: FakeTransport) -> None:
    transport.queue(scheduled())

    send_sms(TO, TEXT, send_at=datetime(2026, 11, 16, 7, 0, tzinfo=UTC))

    assert transport.last_json()["sendAt"] == "2026-11-16T07:00:00+00:00"


def test_an_iso_string_goes_out_as_it_is(transport: FakeTransport) -> None:
    transport.queue(scheduled())

    send_sms(TO, TEXT, send_at="2026-11-16T09:00:00+02:00")

    assert transport.last_json()["sendAt"] == "2026-11-16T09:00:00+02:00"


def test_a_string_without_an_offset_is_refused(transport: FakeTransport) -> None:
    with pytest.raises(ValueError, match="send_at must be an ISO 8601 date and time with its offset"):
        send_sms(TO, TEXT, send_at="2026-11-16T09:00:00")

    assert transport.requests == []


@override_settings(SMSTUNNEL_DEVICE_ID="phone-1", SMSTUNNEL_DEFAULT_COUNTRY_CODE="+40")
def test_the_settings_and_the_key_apply_to_a_scheduled_text(transport: FakeTransport) -> None:
    transport.queue(scheduled())

    send_sms("0721123456", TEXT, idempotency_key="booking-881-reminder", send_at="2026-11-16T09:00:00+02:00")

    assert transport.last.headers["Idempotency-Key"] == "booking-881-reminder"
    assert transport.last_json() == {
        "to": "0721123456",
        "message": TEXT,
        "deviceId": "phone-1",
        "defaultCountryCode": "+40",
        "sendAt": "2026-11-16T09:00:00+02:00",
    }


def test_a_replayed_scheduled_text_says_so(transport: FakeTransport) -> None:
    item = scheduled_text()
    body = {"success": True, "scheduled": True, "scheduledId": item["id"], "sendAt": item["sendAt"], "data": item}
    transport.queue(answer(200, body, {"idempotent-replayed": "true"}))

    text = send_sms(TO, TEXT, idempotency_key="booking-881-reminder", send_at="2026-11-16T09:00:00+02:00")

    assert text.replayed is True


def test_without_send_at_the_result_is_a_send_result_as_before(transport: FakeTransport) -> None:
    transport.queue(sent())

    result = send_sms(TO, TEXT)

    assert isinstance(result, SendResult)
    assert "sendAt" not in transport.last_json()


def test_asend_sms_schedules_from_async_code(transport: FakeTransport) -> None:
    transport.queue(scheduled())

    text = asyncio.run(asend_sms(TO, TEXT, idempotency_key="table-9", send_at="2026-11-16T09:00:00+02:00"))

    assert isinstance(text, ScheduledText)
    assert transport.last.headers["Idempotency-Key"] == "table-9"
    assert transport.last_json()["sendAt"] == "2026-11-16T09:00:00+02:00"


def test_asend_sms_refuses_a_naive_datetime_too(transport: FakeTransport) -> None:
    with pytest.raises(ValueError, match="USE_TZ is True"):
        asyncio.run(asend_sms(TO, TEXT, send_at=datetime(2026, 11, 16, 9, 0)))

    assert transport.requests == []


def test_list_scheduled_reads_what_waits(transport: FakeTransport) -> None:
    page = {"items": [scheduled_text()], "total": 1, "page": 1, "limit": 50, "waiting": 4, "max": 200}
    transport.queue(answer(200, {"success": True, "data": page}))

    texts = list_scheduled()

    assert transport.last.method == "GET"
    assert transport.last.url == "https://smstunnel.test/api/v1/sms/scheduled?scope=waiting&page=1&limit=50"
    assert isinstance(texts, ScheduledTexts)
    assert [text.id for text in texts] == [SCHEDULED_ID]
    assert (texts.waiting, texts.max) == (4, 200)


def test_list_scheduled_takes_the_scope_and_the_page(transport: FakeTransport) -> None:
    page = {"items": [scheduled_text("sent", messageId="6740f1c2a8b9c0d1e2f3a4b5")], "total": 1, "page": 2, "limit": 10}
    transport.queue(answer(200, {"success": True, "data": page}))

    texts = list_scheduled("done", page=2, limit=10)

    assert transport.last.url.endswith("/api/v1/sms/scheduled?scope=done&page=2&limit=10")
    assert texts.items[0].message_id == "6740f1c2a8b9c0d1e2f3a4b5"


def test_cancel_scheduled_sends_a_delete_without_a_key(transport: FakeTransport) -> None:
    transport.queue(answer(200, {"success": True, "data": scheduled_text("canceled")}))

    text = cancel_scheduled(SCHEDULED_ID)

    request = transport.last
    assert request.method == "DELETE"
    assert request.url == f"https://smstunnel.test/api/v1/sms/scheduled/{SCHEDULED_ID}"
    assert request.body is None
    assert "Idempotency-Key" not in request.headers
    assert "django-smstunnel/" in request.headers["User-Agent"]
    assert text.status == ScheduledText.CANCELED


def test_the_async_helpers_list_and_cancel(transport: FakeTransport) -> None:
    page = {"items": [], "total": 0, "page": 1, "limit": 5, "waiting": 0, "max": 10}
    transport.queue(
        answer(200, {"success": True, "data": page}), answer(200, {"success": True, "data": scheduled_text("canceled")})
    )

    texts = asyncio.run(alist_scheduled(limit=5))
    text = asyncio.run(acancel_scheduled(SCHEDULED_ID))

    assert len(texts) == 0
    assert text.status == ScheduledText.CANCELED
    assert [request.method for request in transport.requests] == ["GET", "DELETE"]


def test_get_client_reads_one_scheduled_text(transport: FakeTransport) -> None:
    transport.queue(answer(200, {"success": True, "data": scheduled_text()}))

    text = get_client().get_scheduled(SCHEDULED_ID)

    assert transport.last.url.endswith(f"/api/v1/sms/scheduled/{SCHEDULED_ID}")
    assert text.id == SCHEDULED_ID


def test_a_full_plan_is_not_retryable(transport: FakeTransport) -> None:
    transport.queue(
        answer(
            403,
            {
                "statusCode": 403,
                "error": "Forbidden",
                "code": "SCHEDULE_LIMIT_REACHED",
                "message": "You have as many scheduled texts as your plan allows.",
                "max": 10,
            },
        )
    )

    with pytest.raises(PermissionDeniedError) as caught:
        send_sms(TO, TEXT, send_at="2026-11-16T09:00:00+02:00")

    assert caught.value.error_code == "SCHEDULE_LIMIT_REACHED"
    assert caught.value.body["max"] == 10
    assert not is_retryable(caught.value)


@pytest.mark.parametrize("code", ["SCHEDULE_TOO_SOON", "SCHEDULE_TOO_FAR", "SCHEDULE_INVALID_INPUT"])
def test_a_refused_time_is_a_400_that_is_not_retryable(transport: FakeTransport, code: str) -> None:
    transport.queue(error(400, f"Words for {code}", code=code))

    with pytest.raises(ApiError) as caught:
        send_sms(TO, TEXT, send_at="2026-11-16T09:00:00+02:00")

    assert caught.value.status_code == 400
    assert caught.value.error_code == code
    assert not is_retryable(caught.value)


def test_a_text_that_already_went_out_cannot_be_canceled_and_is_not_retryable(transport: FakeTransport) -> None:
    transport.queue(error(409, "This text has already gone out or was canceled.", code="SCHEDULE_NOT_EDITABLE"))

    with pytest.raises(ApiError) as caught:
        cancel_scheduled(SCHEDULED_ID)

    assert caught.value.status_code == 409
    assert caught.value.error_code == "SCHEDULE_NOT_EDITABLE"
    assert not is_retryable(caught.value), "unlike a 409 for an idempotency key still in use"


def test_the_body_reaches_the_server_as_json(transport: FakeTransport) -> None:
    transport.queue(scheduled())

    send_sms(TO, "Ți-am lăsat cheia la vecini", send_at="2026-11-16T09:00:00+02:00")

    body = transport.last.body
    assert body is not None
    assert json.loads(body)["message"] == "Ți-am lăsat cheia la vecini"
