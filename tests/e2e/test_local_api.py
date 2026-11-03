"""Against a real SMSTunnel test server, never production. Skipped unless SMSTUNNEL_E2E_KEY_FILE is set.

SMSTUNNEL_E2E_KEY_FILE=/path/key.json   {"serverUrl": "http://127.0.0.1:3141", "apiKey": "...", "deviceId": "..."}
                                        a test key with sms:send and sms:read
SMSTUNNEL_E2E_KEY_NAME=frameworks       optional: use keys.<name> from the file instead of apiKey
SMSTUNNEL_E2E_SERVER_URL=http://...     optional: instead of serverUrl (e.g. http://host.docker.internal:3141)
SMSTUNNEL_E2E_MONGO_URL=mongodb://...   optional: the test server's database, to check each text was stored
SMSTUNNEL_E2E_TO=+14155550123           optional: the recipient

About ten requests a run. Every send carries an idempotency key, so one 429 is waited out and retried safely.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar
from urllib.parse import urlsplit

import pytest
from django.test import override_settings

from django_smstunnel import (
    AuthenticationError,
    MessageStatus,
    RateLimitError,
    get_client,
    send_sign_in_code,
    send_sms,
)
from django_smstunnel.two_factor import SmsTunnel

pytestmark = pytest.mark.e2e

KEY_FILE = os.environ.get("SMSTUNNEL_E2E_KEY_FILE", "")
if not KEY_FILE:
    pytest.skip("Set SMSTUNNEL_E2E_KEY_FILE to run against a test server.", allow_module_level=True)

T = TypeVar("T")


class Server:
    def __init__(self) -> None:
        data = json.loads(Path(KEY_FILE).read_text(encoding="utf-8"))
        name = os.environ.get("SMSTUNNEL_E2E_KEY_NAME", "")
        self.api_key: str = data["keys"][name] if name else data["apiKey"]
        self.url: str = os.environ.get("SMSTUNNEL_E2E_SERVER_URL") or data["serverUrl"]
        self.device_id: str | None = data.get("deviceId")
        self.to = os.environ.get("SMSTUNNEL_E2E_TO", "+14155550123")
        self.mongo_url = os.environ.get("SMSTUNNEL_E2E_MONGO_URL", "")

        host = (urlsplit(self.url).hostname or "").lower()
        if host == "smstunnel.io" or host.endswith(".smstunnel.io"):
            pytest.fail("The end-to-end tests never run against production. Point them at a test server.")


@pytest.fixture(scope="module")
def server() -> Server:
    return Server()


@pytest.fixture(autouse=True)
def project_settings(server: Server) -> Iterator[None]:
    with override_settings(
        SMSTUNNEL_API_KEY=server.api_key,
        SMSTUNNEL_BASE_URL=server.url,
        SMSTUNNEL_DEVICE_ID=server.device_id,
        SMSTUNNEL_DEFAULT_COUNTRY_CODE=None,
    ):
        yield


@pytest.fixture(scope="module")
def stored(server: Server) -> Iterator[Callable[[str], dict[str, Any]] | None]:
    """Reads a text from the test server's database (read only), when SMSTUNNEL_E2E_MONGO_URL is set."""
    if not server.mongo_url:
        yield None
        return
    pymongo = pytest.importorskip("pymongo")
    bson = pytest.importorskip("bson")
    mongo = pymongo.MongoClient(server.mongo_url, serverSelectionTimeoutMS=5000)
    messages = mongo.get_default_database()["messages"]

    def read(message_id: str) -> dict[str, Any]:
        document = messages.find_one({"_id": bson.ObjectId(message_id)})
        assert document is not None, f"Text {message_id} is not in the database."
        document["_copies"] = messages.count_documents({"message": document["message"]})
        return dict(document)

    try:
        yield read
    finally:
        mongo.close()


def once_more_after_429(call: Callable[[], T]) -> T:
    """The test server is shared by several test suites: one 429 is waited out (the send carries a key)."""
    try:
        return call()
    except RateLimitError as error:
        time.sleep(min(65.0, max(1.0, error.retry_after or 15.0)))
        return call()


def wait_until_sent(message_id: str) -> MessageStatus:
    time.sleep(1.5)
    status = get_client().get_message_status(message_id)
    for _ in range(4):
        if status.status != MessageStatus.PENDING:
            break
        time.sleep(2)
        status = get_client().get_message_status(message_id)
    return status


def check_stored(
    stored: Callable[[str], dict[str, Any]] | None, message_id: str, to: str, text: str, server: Server
) -> None:
    if stored is None:
        return
    document = stored(message_id)
    assert document["type"] == "outgoing"
    assert document["phoneNumber"] == to
    assert document["message"] == text
    assert document["status"] in ("sent", "delivered")
    assert document["_copies"] == 1, "The text was stored more than once."
    if server.device_id:
        assert str(document["deviceId"]) == server.device_id


def test_the_test_phone_is_linked_and_online(server: Server) -> None:
    phones = get_client().get_devices()

    assert phones.online > 0, "No phone is online on the test server."
    if server.device_id:
        phone = phones.find(server.device_id)
        assert phone is not None, "The test phone is not linked to this key's account."
        assert phone.is_online, "The test phone is offline."


def test_send_sms_reaches_the_phone_once_even_when_retried(
    server: Server, stored: Callable[[str], dict[str, Any]] | None
) -> None:
    text = f"SMSTunnel for Django end-to-end test {secrets.token_hex(4)}"
    key = f"django-smstunnel-e2e-{secrets.token_hex(8)}"

    first = once_more_after_429(lambda: send_sms(server.to, text, idempotency_key=key))
    retry = once_more_after_429(lambda: send_sms(server.to, text, idempotency_key=key))

    assert len(first.message_id) == 24
    assert first.queued is False, "The phone was offline, so the text was queued."
    assert retry.message_id == first.message_id, "The retry with the same key sent a second text."
    status = wait_until_sent(first.message_id)
    assert status.phone_number == server.to
    assert status.status in (MessageStatus.SENT, MessageStatus.DELIVERED)
    check_stored(stored, first.message_id, server.to, text, server)


def test_send_sign_in_code_reaches_the_phone(server: Server, stored: Callable[[str], dict[str, Any]] | None) -> None:
    text = f"Your SMSTunnel for Django test code is {secrets.randbelow(900000) + 100000}"
    key = f"django-smstunnel-e2e-{secrets.token_hex(8)}"

    result = once_more_after_429(lambda: send_sign_in_code(server.to, text, idempotency_key=key))

    status = wait_until_sent(result.message_id)
    assert status.status in (MessageStatus.SENT, MessageStatus.DELIVERED)
    check_stored(stored, result.message_id, server.to, text, server)


def test_django_two_factor_auth_codes_go_out(server: Server, stored: Callable[[str], dict[str, Any]] | None) -> None:
    token = str(secrets.randbelow(900000) + 100000)
    key = f"django-smstunnel-e2e-{secrets.token_hex(8)}"

    class WithKey(SmsTunnel):
        def idempotency_key(self, device: Any, token: str) -> str:
            return key

    once_more_after_429(lambda: WithKey().send_sms(device=SimpleNamespace(number=server.to), token=token))

    # The class answers nothing to django-two-factor-auth. The same request with the same key gets the first
    # answer back (with the message id), and no second text.
    text = f"Your OTP token is {token}"
    replay = once_more_after_429(lambda: send_sign_in_code(server.to, text, idempotency_key=key))
    status = wait_until_sent(replay.message_id)
    assert status.phone_number == server.to
    assert status.status in (MessageStatus.SENT, MessageStatus.DELIVERED)
    check_stored(stored, replay.message_id, server.to, text, server)


def test_a_wrong_key_is_refused(server: Server) -> None:
    with override_settings(SMSTUNNEL_API_KEY="sk_live_" + "0" * 32), pytest.raises(AuthenticationError) as caught:
        get_client().get_devices()

    assert caught.value.status_code == 401
