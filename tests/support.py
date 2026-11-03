"""A transport that answers like SMSTunnel without a network, and records what the client sent."""

from __future__ import annotations

import json
from collections import deque
from typing import Any

from smstunnel.transport import HttpRequest, HttpResponse

MESSAGE_ID = "6740f1c2a8b9c0d1e2f3a4b5"


def answer(status: int, body: Any = None, headers: dict[str, str] | None = None) -> HttpResponse:
    text = "" if body is None else body if isinstance(body, str) else json.dumps(body)
    return HttpResponse(status, text, {"content-type": "application/json", **(headers or {})})


def sent(message_id: str = MESSAGE_ID, *, queued: bool = False) -> HttpResponse:
    return answer(
        200,
        {
            "success": True,
            "messageId": message_id,
            "queued": queued,
            "data": {
                "messageId": message_id,
                "branded": False,
                "queued": queued,
                "remaining": {"clean": 99, "total": "unlimited"},
            },
        },
    )


def error(status: int, message: str, *, code: str | None = None, headers: dict[str, str] | None = None) -> HttpResponse:
    labels = {400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 409: "Conflict", 429: "Too Many Requests"}
    body: dict[str, Any] = {"statusCode": status, "error": labels.get(status, "Error"), "message": message}
    if code:
        body["code"] = code
    return answer(status, body, headers)


class FakeTransport:
    """Hands back the queued answers in order (an exception is raised instead), and records every request."""

    def __init__(self) -> None:
        self.requests: list[HttpRequest] = []
        self._answers: deque[HttpResponse | Exception] = deque()

    def queue(self, *answers: HttpResponse | Exception) -> None:
        self._answers.extend(answers)

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        reply = self._answers.popleft() if self._answers else sent()
        if isinstance(reply, Exception):
            raise reply
        return reply

    @property
    def last(self) -> HttpRequest:
        assert self.requests, "No request was sent."
        return self.requests[-1]

    def last_json(self) -> dict[str, Any]:
        body = self.last.body
        assert body is not None, "The last request had no body."
        data = json.loads(body)
        assert isinstance(data, dict)
        return data
