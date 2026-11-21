"""The part the sign-in integrations share. These tests need none of the host packages."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.core.checks import Error
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory, override_settings
from smstunnel import TransportError

from django_smstunnel import client as client_module
from django_smstunnel import signin
from django_smstunnel.checks import check_signin
from django_smstunnel.signin import ClientIPMiddleware, CodeResult, Rate, client_ip, code_text, limits, send_code

from .support import FakeTransport, error

NUMBER = "+14155550123"
TEXT = "Your sign-in code is 482913"


@pytest.fixture(autouse=True)
def clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def request_from(remote: str, **headers: Any) -> HttpRequest:
    return RequestFactory().post("/", REMOTE_ADDR=remote, **headers)


# Numbers


@pytest.mark.parametrize("number", ["+14155550123", "+40721123456", "+1234567", "+123456789012345"])
def test_international_numbers_are_accepted(number: str) -> None:
    assert signin.is_international(number)


@pytest.mark.parametrize(
    "number",
    [
        "14155550123",  # no +
        "0721123456",
        "+04155550123",  # the way around the premium-number block
        "+0",
        "+123456",  # 6 digits
        "+1234567890123456",  # 16 digits
        "+1415 555 0123",
        "+1415555012a",
        "+14155550123\n",
        "+١٤١٥٥٥٥٠١٢٣",  # digits, in another script
        "",
        None,
        14155550123,
    ],
)
def test_anything_else_is_refused_before_any_request(transport: FakeTransport, number: Any) -> None:
    assert not signin.is_international(number)

    assert send_code(number, TEXT) is CodeResult.BAD_NUMBER
    assert transport.requests == []


# The send


def test_a_code_goes_once_through_the_sign_in_route(transport: FakeTransport) -> None:
    assert send_code(NUMBER, TEXT) is CodeResult.SENT

    assert len(transport.requests) == 1
    assert transport.last.url == "https://smstunnel.test/api/v1/sms/send-2fa"
    assert transport.last_json() == {"to": NUMBER, "message": TEXT}


def test_the_idempotency_key_is_stable_and_reveals_nothing(transport: FakeTransport) -> None:
    send_code(NUMBER, TEXT)
    first = transport.last.headers["Idempotency-Key"]
    send_code(NUMBER, TEXT)
    again = transport.last.headers["Idempotency-Key"]
    send_code(NUMBER, "Your sign-in code is 111222")
    other = transport.last.headers["Idempotency-Key"]

    assert first == again
    assert first != other
    assert first.startswith("signin-")
    assert len(first) <= 255
    assert "482913" not in first
    assert "4155550123" not in first


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (error(400, "Invalid 2FA message"), CodeResult.FAILED),
        (error(401, "Invalid or expired API key"), CodeResult.FAILED),
        (error(403, "Forbidden", code="PERMISSION_REQUIRED"), CodeResult.FAILED),
        (error(422, "Reused", code="IDEMPOTENCY_KEY_REUSED"), CodeResult.FAILED),
        (error(429, "Daily limit reached", code="KEY_DAILY_CAP_REACHED"), CodeResult.FAILED),
        (error(429, "Too Many Requests", headers={"Retry-After": "1"}), CodeResult.FAILED),
        (error(409, "In progress", code="IDEMPOTENCY_KEY_IN_PROGRESS"), CodeResult.UNCERTAIN),
        (error(500, "Internal error"), CodeResult.UNCERTAIN),
        (error(503, "Unavailable"), CodeResult.UNCERTAIN),
        (TransportError("timed out"), CodeResult.UNCERTAIN),
    ],
)
def test_no_failure_is_ever_retried(transport: FakeTransport, answer: Any, expected: CodeResult) -> None:
    transport.queue(answer)

    assert send_code(NUMBER, TEXT) is expected
    assert len(transport.requests) == 1


@override_settings(SMSTUNNEL_API_KEY="")
def test_a_missing_key_fails_quietly(transport: FakeTransport) -> None:
    assert send_code(NUMBER, TEXT) is CodeResult.FAILED
    assert transport.requests == []


@pytest.mark.parametrize(
    "answer",
    [
        error(400, f"Invalid phone number {NUMBER} in {TEXT}"),
        error(401, f"Bad key sk_test_unit for {NUMBER}"),
        error(500, f"Crashed on {NUMBER}: {TEXT}"),
        TransportError(f"Connection to {NUMBER} lost: {TEXT}"),
        RuntimeError(f"Unexpected {NUMBER} {TEXT}"),
    ],
)
def test_the_log_never_holds_the_number_the_code_or_the_key(
    transport: FakeTransport, caplog: pytest.LogCaptureFixture, answer: Any
) -> None:
    transport.queue(answer)

    with caplog.at_level(logging.DEBUG):
        send_code(NUMBER, TEXT)

    assert caplog.records, "A failed send must be logged."
    for record in caplog.records:
        logged = record.getMessage()
        assert record.exc_info is None
        for secret in (NUMBER, "4155550123", "482913", "sk_test_unit"):
            assert secret not in logged


def test_a_background_send_returns_at_once_and_sends_once(
    monkeypatch: pytest.MonkeyPatch, transport: FakeTransport
) -> None:
    release = threading.Event()
    done = threading.Event()
    real = transport.send

    def slow(request: Any) -> Any:
        release.wait(5)
        answer = real(request)
        done.set()
        return answer

    monkeypatch.setattr(transport, "send", slow)

    assert send_code(NUMBER, TEXT, background=True) is CodeResult.BACKGROUND
    assert transport.requests == [], "The caller must not wait for the send."

    release.set()
    assert done.wait(5)
    assert len(transport.requests) == 1


def test_a_failed_background_send_is_only_logged(transport: FakeTransport, caplog: pytest.LogCaptureFixture) -> None:
    transport.queue(error(401, "Invalid or expired API key"))

    with caplog.at_level(logging.ERROR, logger="django_smstunnel.signin"):
        assert send_code(NUMBER, TEXT, background=True) is CodeResult.BACKGROUND
        assert signin._pool.drain(5)

    assert "HTTP 401" in caplog.text


def test_when_no_thread_can_start_nothing_is_sent(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> None:
    def refuse(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(signin, "_pool", signin._SendPool())
    monkeypatch.setattr(threading.Thread, "start", refuse)

    assert send_code(NUMBER, TEXT, background=True) is CodeResult.FAILED
    assert transport.requests == []


# The bounded worker pool


NO_LIMITS: dict[str, str | None] = {"number": None, "ip": None, "total": None}


def nth_number(index: int) -> str:
    return f"+1415555{index:04d}"


@pytest.fixture
def held_sends(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> Iterator[SimpleNamespace]:
    """A fresh pool whose sends wait until released, like an API that stopped answering."""
    entered = threading.Semaphore(0)
    release = threading.Event()
    real = transport.send

    def held(request: Any) -> Any:
        entered.release()
        release.wait(30)
        return real(request)

    monkeypatch.setattr(transport, "send", held)
    monkeypatch.setattr(signin, "_pool", signin._SendPool())
    yield SimpleNamespace(entered=entered, release=release)
    release.set()
    assert signin._pool.drain(10)


def fill_the_pool(held: SimpleNamespace) -> None:
    """Every worker busy and the queue full."""
    for index in range(signin._WORKERS):
        assert send_code(nth_number(index), TEXT, background=True) is CodeResult.BACKGROUND
    for _ in range(signin._WORKERS):
        assert held.entered.acquire(timeout=5)
    for index in range(signin._QUEUE_SIZE):
        assert send_code(nth_number(1000 + index), TEXT, background=True) is CodeResult.BACKGROUND


@override_settings(SMSTUNNEL_SIGNIN_LIMITS=NO_LIMITS)
def test_a_flood_uses_a_fixed_number_of_threads(held_sends: SimpleNamespace, transport: FakeTransport) -> None:
    before = threading.active_count()

    fill_the_pool(held_sends)
    results = [send_code(nth_number(index), TEXT, background=True) for index in range(2000, 2500)]

    assert set(results) == {CodeResult.LIMITED}
    assert threading.active_count() == before + signin._WORKERS
    held_sends.release.set()
    assert signin._pool.drain(10)
    assert len(transport.requests) == signin._WORKERS + signin._QUEUE_SIZE, "What was refused is never sent later."


@override_settings(SMSTUNNEL_SIGNIN_LIMITS=NO_LIMITS)
def test_a_full_queue_never_makes_the_visitor_wait(held_sends: SimpleNamespace) -> None:
    fill_the_pool(held_sends)

    started = time.monotonic()
    for _ in range(200):
        send_code(NUMBER, TEXT, background=True)

    assert time.monotonic() - started < 1


@override_settings(SMSTUNNEL_SIGNIN_LIMITS=NO_LIMITS)
def test_a_full_queue_tells_the_owner_without_flooding_the_log(
    held_sends: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    fill_the_pool(held_sends)

    with caplog.at_level(logging.ERROR, logger="django_smstunnel.signin"):
        for _ in range(50):
            send_code(NUMBER, TEXT, background=True)
        signin._pool._last_report = -signin._FULL_LOG_EVERY  # A minute later.
        send_code(NUMBER, TEXT, background=True)

    lines = [record.getMessage() for record in caplog.records if "waiting to go out" in record.getMessage()]
    assert [line.split(" sign-in")[0] for line in lines] == ["SMSTunnel: 1", "SMSTunnel: 50"]
    assert NUMBER not in caplog.text
    assert "482913" not in caplog.text


@override_settings(SMSTUNNEL_SIGNIN_LIMITS=NO_LIMITS)
def test_a_worker_survives_an_unexpected_failure(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> None:
    monkeypatch.setattr(signin, "_pool", signin._SendPool())
    real = signin._limit_and_deliver
    calls: list[str] = []

    def fails_once(to: str, text: str, ip: str | None) -> CodeResult:
        calls.append(to)
        if len(calls) == 1:
            raise KeyError(to)
        return real(to, text, ip)

    monkeypatch.setattr(signin, "_limit_and_deliver", fails_once)
    send_code(NUMBER, TEXT, background=True)
    send_code(NUMBER, TEXT, background=True)

    assert signin._pool.drain(5)
    assert len(calls) == 2
    assert len(transport.requests) == 1
    assert all(worker.is_alive() for worker in signin._pool._workers)


PROCESS_SETUP = """
import os, time
import django
from django.conf import settings
settings.configure(SECRET_KEY="x" * 50, SMSTUNNEL_API_KEY="sk_test_unit", SMSTUNNEL_BASE_URL="https://smstunnel.test")
django.setup()
from django_smstunnel import client, signin
from tests.support import sent
NUMBER, TEXT = "+14155550123", "Your sign-in code is 482913"
"""


def in_a_new_process(body: str) -> list[str]:
    command = [sys.executable, "-c", PROCESS_SETUP + body]
    root = Path(__file__).resolve().parent.parent
    done = subprocess.run(command, capture_output=True, text=True, check=True, cwd=root, timeout=20)  # noqa: S603
    return done.stdout.split()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Needs os.fork.")
def test_a_forked_process_does_not_inherit_the_queue() -> None:
    output = in_a_new_process(
        """
parent = signin._pool
parent._queue.put_nowait((NUMBER, TEXT, None))
pid = os.fork()
if pid == 0:
    os._exit(0 if signin._pool is not parent and signin._pool._queue.empty() else 1)
print(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
parent._queue.get_nowait()
parent._queue.task_done()
"""
    )

    assert output == ["0"]


def test_a_code_in_flight_when_the_process_exits_still_goes_out() -> None:
    output = in_a_new_process(
        """
class Slow:
    def send(self, request):
        time.sleep(0.3)
        print("sent", flush=True)
        return sent()
client._make_transport = Slow
print(signin.send_code(NUMBER, TEXT, background=True).value, flush=True)
"""
    )

    assert output == ["background", "sent"]


def test_a_hung_send_does_not_hold_up_the_exit() -> None:
    started = time.monotonic()

    output = in_a_new_process(
        """
signin._EXIT_WAIT = 0.2
class Hung:
    def send(self, request):
        time.sleep(60)
client._make_transport = Hung
print(signin.send_code(NUMBER, TEXT, background=True).value, flush=True)
"""
    )

    assert output == ["background"]
    assert time.monotonic() - started < 15


# Limits


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "2/h", "ip": None, "total": None})
def test_the_limit_per_number(transport: FakeTransport) -> None:
    results = [send_code(NUMBER, f"Your sign-in code is 10000{i}") for i in range(4)]

    assert results == [CodeResult.SENT, CodeResult.SENT, CodeResult.LIMITED, CodeResult.LIMITED]
    assert len(transport.requests) == 2
    assert send_code("+14155550124", TEXT) is CodeResult.SENT, "Another number has its own allowance."


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "2/h", "total": None})
def test_the_limit_per_ip(transport: FakeTransport) -> None:
    results = [send_code(f"+1415555012{i}", TEXT, ip="203.0.113.7") for i in range(4)]

    assert results == [CodeResult.SENT, CodeResult.SENT, CodeResult.LIMITED, CodeResult.LIMITED]
    assert send_code("+14155550129", TEXT, ip="203.0.113.8") is CodeResult.SENT, "Another IP has its own."


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": None, "total": "3/d"})
def test_the_limit_for_the_whole_project(transport: FakeTransport) -> None:
    results = [send_code(f"+1415555012{i}", TEXT, ip=f"203.0.113.{i}") for i in range(5)]

    assert results.count(CodeResult.SENT) == 3
    assert len(transport.requests) == 3


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "2/h", "ip": "1/h", "total": None})
def test_a_flood_from_one_ip_does_not_use_up_the_numbers_allowance(transport: FakeTransport) -> None:
    assert send_code(NUMBER, TEXT, ip="203.0.113.7") is CodeResult.SENT
    for _ in range(5):
        assert send_code(NUMBER, TEXT, ip="203.0.113.7") is CodeResult.LIMITED

    assert send_code(NUMBER, TEXT, ip="198.51.100.1") is CodeResult.SENT


def test_the_default_limits_stop_a_flood_to_one_number(transport: FakeTransport) -> None:
    sent = sum(send_code(NUMBER, f"Your sign-in code is 1000{i:02}") is CodeResult.SENT for i in range(20))

    assert sent == 5


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "1/h", "ip": None, "total": None})
def test_a_new_window_starts_over(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> None:
    moment = [7_200_000.0]
    monkeypatch.setattr("time.time", lambda: moment[0])

    assert send_code(NUMBER, TEXT) is CodeResult.SENT
    assert send_code(NUMBER, TEXT) is CodeResult.LIMITED
    moment[0] += 3600
    assert send_code(NUMBER, TEXT) is CodeResult.SENT


def test_counters_hold_no_number_or_ip(transport: FakeTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    keys: list[str] = []
    real_add = cache.add

    def recording_add(key: str, *args: Any, **kwargs: Any) -> bool:
        keys.append(key)
        return real_add(key, *args, **kwargs)

    monkeypatch.setattr(cache, "add", recording_add)
    send_code(NUMBER, TEXT, ip="203.0.113.7")

    assert keys
    assert not any("4155550123" in key or "203.0.113.7" in key for key in keys)


def test_when_the_cache_is_down_nothing_is_sent(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> None:
    def down(*args: object, **kwargs: object) -> None:
        raise ConnectionError("cache down")

    monkeypatch.setattr(cache, "add", down)

    assert send_code(NUMBER, TEXT) is CodeResult.FAILED
    assert transport.requests == []


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"nonsense": "1/h"})
def test_a_wrong_limit_setting_sends_nothing_and_does_not_raise(transport: FakeTransport) -> None:
    assert send_code(NUMBER, TEXT) is CodeResult.FAILED
    assert transport.requests == []


def test_limits_are_read_from_the_setting() -> None:
    assert limits()["number"] == (Rate(5, 3600), Rate(10, 86400))
    with override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "3/15m, 7/2d", "ip": None}):
        configured = limits()
    assert configured["number"] == (Rate(3, 900), Rate(7, 172800))
    assert configured["ip"] == ()
    assert configured["total"] == (Rate(100, 3600), Rate(300, 86400))


@pytest.mark.parametrize(
    "value",
    ["5/h", {"number": "five"}, {"number": "5/y"}, {"number": "5/0h"}, {"number": 5}, {"people": "1/h"}],
)
def test_a_limit_it_cannot_read_is_an_error(value: Any) -> None:
    if value == "5/h":
        value = ["5/h"]
    with override_settings(SMSTUNNEL_SIGNIN_LIMITS=value), pytest.raises(ImproperlyConfigured):
        limits()


# A forged X-Forwarded-For

FORGED_HEADERS: dict[str, Callable[[int], dict[str, str]]] = {
    "no header": lambda index: {},
    "a fixed value": lambda index: {"HTTP_X_FORWARDED_FOR": "8.8.4.4"},
    "a new value each time": lambda index: {"HTTP_X_FORWARDED_FOR": f"8.8.{index}.4"},
    "a chain": lambda index: {"HTTP_X_FORWARDED_FOR": f"1.1.1.{index}, 8.8.8.8"},
    "not an address": lambda index: {"HTTP_X_FORWARDED_FOR": "evil"},
    "empty": lambda index: {"HTTP_X_FORWARDED_FOR": ""},
    "very long": lambda index: {"HTTP_X_FORWARDED_FOR": "9.9.9.9, " * 500},
}
PEERS = ["10.0.0.5", "127.0.0.1", "::1", "::ffff:10.0.0.5", "100.64.0.5", "8.8.8.8", "::ffff:8.8.8.8"]


def through_the_site(peer: str, forged: str, numbers: list[str]) -> list[CodeResult]:
    """One request per number, from ``peer``, each with the forged header."""
    results: list[CodeResult] = []
    remaining = iter(numbers)

    def view(request: HttpRequest) -> HttpResponse:
        results.append(send_code(next(remaining), TEXT))
        return HttpResponse()

    middleware = ClientIPMiddleware(view)
    for index in range(len(numbers)):
        middleware(request_from(peer, **FORGED_HEADERS[forged](index)))
    return results


@pytest.mark.parametrize("peer", PEERS)
@pytest.mark.parametrize("forged", FORGED_HEADERS)
def test_a_forged_header_never_changes_the_limit_per_number(transport: FakeTransport, peer: str, forged: str) -> None:
    with override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "2/h", "ip": "50/h", "total": None}):
        results = through_the_site(peer, forged, [NUMBER] * 6)

    assert results == [CodeResult.SENT] * 2 + [CodeResult.LIMITED] * 4
    assert len(transport.requests) == 2


@pytest.mark.parametrize("peer", PEERS)
@pytest.mark.parametrize("forged", FORGED_HEADERS)
def test_a_forged_header_never_changes_the_project_limit(transport: FakeTransport, peer: str, forged: str) -> None:
    with override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "50/h", "total": "3/h"}):
        results = through_the_site(peer, forged, [nth_number(index) for index in range(6)])

    assert results == [CodeResult.SENT] * 3 + [CodeResult.LIMITED] * 3
    assert len(transport.requests) == 3


@pytest.mark.parametrize("peer", ["8.8.8.8", "::ffff:8.8.8.8"])
@pytest.mark.parametrize("forged", ["a fixed value", "a new value each time", "a chain"])
def test_from_a_public_address_a_header_cant_escape_the_ip_limit(
    transport: FakeTransport, peer: str, forged: str
) -> None:
    with override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "1/h", "total": None}):
        results = through_the_site(peer, forged, [nth_number(index) for index in range(3)])

    assert results == [CodeResult.SENT, CodeResult.LIMITED, CodeResult.LIMITED]


def test_from_a_private_address_the_ip_limit_is_off_and_the_log_says_so(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(signin, "_warned_about_proxy", False)

    with (
        override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "1/h", "total": None}),
        caplog.at_level(logging.WARNING, logger="django_smstunnel.signin"),
    ):
        results = through_the_site("10.0.0.5", "a fixed value", [nth_number(index) for index in range(3)])

    assert results == [CodeResult.SENT] * 3
    assert caplog.text.count("the per-IP limit on sign-in codes is OFF") == 1
    assert "until you set SMSTUNNEL_CLIENT_IP" in caplog.text


def test_setting_the_client_ip_turns_the_ip_limit_back_on(transport: FakeTransport) -> None:
    with override_settings(
        SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "1/h", "total": None},
        SMSTUNNEL_CLIENT_IP=lambda request: "8.8.8.8",
    ):
        results = through_the_site("10.0.0.5", "a new value each time", [nth_number(index) for index in range(3)])

    assert results == [CodeResult.SENT, CodeResult.LIMITED, CodeResult.LIMITED]


# A cache that keeps nothing

DUMMY_CACHE = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}


@override_settings(CACHES=DUMMY_CACHE)
def test_a_cache_that_keeps_nothing_sends_nothing(transport: FakeTransport, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="django_smstunnel.signin"):
        assert send_code(NUMBER, TEXT) is CodeResult.FAILED

    assert transport.requests == []
    assert "the cache keeps nothing" in caplog.text
    assert NUMBER not in caplog.text


@override_settings(CACHES=DUMMY_CACHE, SMSTUNNEL_SIGNIN_LIMITS=NO_LIMITS)
def test_with_every_limit_off_no_counter_is_needed(transport: FakeTransport) -> None:
    assert send_code(NUMBER, TEXT) is CodeResult.SENT


@override_settings(CACHES=DUMMY_CACHE, SMSTUNNEL_SIGNIN_LIMITS={})
def test_a_cache_that_keeps_nothing_leaves_manage_py_working(capsys: pytest.CaptureFixture[str]) -> None:
    call_command("check")

    assert "smstunnel.W006" in capsys.readouterr().err


@override_settings(CACHES=DUMMY_CACHE, SMSTUNNEL_SIGNIN_LIMITS=NO_LIMITS)
def test_no_w006_when_there_is_nothing_to_count() -> None:
    assert check_signin() == []


def test_a_counter_that_expires_while_counting_still_counts(
    transport: FakeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = cache.incr
    calls: list[str] = []

    def expired_once(key: str, *args: Any, **kwargs: Any) -> int:
        calls.append(key)
        if len(calls) == 1:
            cache.delete(key)
            raise ValueError("gone")
        return real(key, *args, **kwargs)

    monkeypatch.setattr(cache, "incr", expired_once)

    assert send_code(NUMBER, TEXT) is CodeResult.SENT
    assert cache.get(calls[0]) == 1


# The visitor's IP


def test_the_ip_is_the_remote_address() -> None:
    assert client_ip(request_from("203.0.113.7")) == "203.0.113.7"


def test_a_forged_forwarded_header_changes_nothing_on_a_direct_connection() -> None:
    assert client_ip(request_from("8.8.8.8", HTTP_X_FORWARDED_FOR="1.1.1.1")) == "8.8.8.8"


@pytest.mark.parametrize("proxy", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "::1"])
def test_behind_a_local_proxy_the_ip_is_unknown_rather_than_shared(proxy: str) -> None:
    assert client_ip(request_from(proxy, HTTP_X_FORWARDED_FOR="198.51.100.9")) is None


@pytest.mark.parametrize(
    ("peer", "counted"),
    [("::ffff:8.8.8.8", "8.8.8.8"), ("::ffff:10.0.0.5", None), ("100.64.0.5", None), ("fd00::1", None)],
)
def test_what_counts_as_a_private_peer(peer: str, counted: str | None) -> None:
    assert client_ip(request_from(peer, HTTP_X_FORWARDED_FOR="1.1.1.1")) == counted


def test_a_private_address_without_a_proxy_counts() -> None:
    assert client_ip(request_from("10.0.0.5")) == "10.0.0.5"


def test_an_ipv6_visitor_counts_as_its_slash_64() -> None:
    assert client_ip(request_from("2001:db8:1:2:aaaa::1")) == client_ip(request_from("2001:db8:1:2:bbbb::2"))
    assert client_ip(request_from("2001:db8:1:2::1")) != client_ip(request_from("2001:db8:1:3::1"))


def test_an_ipv4_mapped_address_is_the_ipv4_address() -> None:
    assert client_ip(request_from("::ffff:203.0.113.7")) == "203.0.113.7"


def test_a_project_can_say_where_the_ip_is() -> None:
    with override_settings(SMSTUNNEL_CLIENT_IP=lambda request: request.META["HTTP_CF_CONNECTING_IP"]):
        assert client_ip(request_from("10.0.0.5", HTTP_CF_CONNECTING_IP="198.51.100.9")) == "198.51.100.9"


def test_a_failing_ip_function_only_skips_the_limit(caplog: pytest.LogCaptureFixture) -> None:
    def broken(request: HttpRequest) -> str:
        raise KeyError("no header")

    with override_settings(SMSTUNNEL_CLIENT_IP=broken):
        assert client_ip(request_from("203.0.113.7")) is None
    assert "SMSTUNNEL_CLIENT_IP" in caplog.text


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "1/h", "total": None})
def test_the_middleware_gives_send_code_the_ip(transport: FakeTransport) -> None:
    results: list[CodeResult] = []

    def view(request: HttpRequest) -> HttpResponse:
        results.append(send_code(NUMBER, TEXT))
        return HttpResponse()

    middleware = ClientIPMiddleware(view)
    middleware(request_from("203.0.113.7"))
    middleware(request_from("203.0.113.7"))
    middleware(request_from("203.0.113.8"))

    assert results == [CodeResult.SENT, CodeResult.LIMITED, CodeResult.SENT]
    assert signin._client_ip.get() is None, "The IP must not outlive the request."


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": None, "ip": "1/h", "total": None})
def test_the_middleware_works_in_async_code(transport: FakeTransport) -> None:
    results: list[CodeResult] = []

    async def view(request: HttpRequest) -> HttpResponse:
        results.append(send_code(NUMBER, TEXT))
        return HttpResponse()

    middleware = ClientIPMiddleware(view)

    async def visit() -> None:
        await middleware(request_from("203.0.113.7"))
        await middleware(request_from("203.0.113.7"))

    asyncio.run(visit())

    assert results == [CodeResult.SENT, CodeResult.LIMITED]


# The words


def test_the_default_words() -> None:
    assert code_text("482913") == "Your sign-in code is 482913"


def test_without_the_template_the_same_words_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.template import TemplateDoesNotExist

    def missing(*args: object, **kwargs: object) -> str:
        raise TemplateDoesNotExist("code.txt")

    monkeypatch.setattr("django_smstunnel.signin.render_to_string", missing)

    assert code_text("482913") == "Your sign-in code is 482913"


# Staying out of the way


def test_a_project_without_the_integrations_gets_no_new_check_messages() -> None:
    assert check_signin() == []


@override_settings(SMSTUNNEL_SIGNIN_LIMITS={"number": "lots"})
def test_a_wrong_limit_setting_is_reported() -> None:
    messages = check_signin()

    assert [message.id for message in messages] == ["smstunnel.E002"]
    assert isinstance(messages[0], Error)


@override_settings(
    SMSTUNNEL_SIGNIN_LIMITS={},
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
)
def test_a_cache_that_counts_nothing_is_a_warning() -> None:
    assert [message.id for message in check_signin()] == ["smstunnel.W006"]


def test_importing_the_package_loads_none_of_the_integrations() -> None:
    code = (
        "import json, sys; import django_smstunnel; "
        "names = ['allauth', 'django_otp', 'phone_verify', 'django_smstunnel.signin', "
        "'django_smstunnel.allauth', 'django_smstunnel.otp', 'django_smstunnel.phone_verify']; "
        "print(json.dumps([name for name in names if name in sys.modules]))"
    )
    command = [sys.executable, "-c", code]
    output = subprocess.run(command, capture_output=True, text=True, check=True).stdout  # noqa: S603

    assert json.loads(output) == []


def test_the_public_names_are_the_ones_of_1_1() -> None:
    import django_smstunnel

    assert sorted(django_smstunnel.__all__) == sorted(
        [
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
    )
    assert client_module.send_sign_in_code is django_smstunnel.send_sign_in_code
