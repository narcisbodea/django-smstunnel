"""Over a real socket: the standard-library transport against a local stub of the SMSTunnel API."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from django.test import override_settings

from django_smstunnel import ApiError, RateLimitError, get_client, is_retryable, retry_after, send_sms


class Stub:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.answers: list[tuple[int, dict[str, Any], dict[str, str]]] = []
        self.url = ""


@pytest.fixture
def stub() -> Iterator[Stub]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.requests.append(
                {"path": self.path, "headers": dict(self.headers.items()), "body": json.loads(self.rfile.read(length))}
            )
            status, body, headers = (
                state.answers.pop(0)
                if state.answers
                else (200, {"success": True, "messageId": "6740f1c2a8b9c0d1e2f3a4b5", "data": {}}, {})
            )
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with override_settings(SMSTUNNEL_BASE_URL=state.url, SMSTUNNEL_API_KEY="sk_test_stub"):
            yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        get_client.cache_clear()


def test_the_headers_reach_the_server(stub: Stub) -> None:
    result = send_sms("+14155550123", "Over the wire", idempotency_key="wire-1")

    [request] = stub.requests
    headers = {name.lower(): value for name, value in request["headers"].items()}
    assert request["path"] == "/api/v1/sms/send"
    assert headers["x-api-key"] == "sk_test_stub"
    assert headers["idempotency-key"] == "wire-1"
    assert "django-smstunnel/" in headers["user-agent"]
    assert request["body"] == {"to": "+14155550123", "message": "Over the wire"}
    assert result.message_id == "6740f1c2a8b9c0d1e2f3a4b5"


def test_retry_after_comes_from_the_answer(stub: Stub) -> None:
    stub.answers.append(
        (429, {"statusCode": 429, "message": "ThrottlerException: Too Many Requests"}, {"Retry-After": "7"})
    )

    with pytest.raises(RateLimitError) as caught:
        send_sms("+14155550123", "Too fast", idempotency_key="wire-2")

    assert retry_after(caught.value) == 7.0
    assert len(stub.requests) == 1


def test_a_server_error_comes_back_once(stub: Stub) -> None:
    stub.answers.append((502, {"statusCode": 502, "message": "Server error"}, {}))

    with pytest.raises(ApiError) as caught:
        send_sms("+14155550123", "Server trouble", idempotency_key="wire-3")

    assert caught.value.status_code == 502
    assert is_retryable(caught.value)
    assert len(stub.requests) == 1
