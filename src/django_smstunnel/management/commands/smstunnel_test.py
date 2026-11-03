from __future__ import annotations

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError, CommandParser
from smstunnel import SmsTunnelError

from ...client import get_client, send_sms
from ...conf import get_settings

DEFAULT_MESSAGE = "This is a test text from SMSTunnel for Django."


class Command(BaseCommand):
    help = (
        "Checks the SMSTunnel setup: the API key and your phones. With --to, it also sends one test text from your "
        "phone."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--to",
            help="Send one test text to this number, in international format (e.g. +14155550123).",
        )
        parser.add_argument("--message", default=DEFAULT_MESSAGE, help="The words of the test text.")

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            conf = get_settings()
            client = get_client()
            phones = client.get_devices()
        except ImproperlyConfigured as error:
            raise CommandError(str(error)) from error
        except SmsTunnelError as error:
            raise CommandError(f"SMSTunnel did not accept the request: {error}") from error

        self.stdout.write(f"The API key works ({client.base_url}).")
        if not phones.total:
            self.stdout.write(
                self.style.WARNING(
                    "No phone is linked to this account yet. Install the SMSTunnel app from Google Play on your "
                    "Android phone and sign in (or scan the QR code from Phones → Add phone in your dashboard)."
                )
            )
        for phone in phones:
            state = self.style.SUCCESS("online") if phone.is_online else self.style.WARNING("offline")
            self.stdout.write(f"  {phone.name} ({phone.id}): {state}")
        if conf.device_id and phones.find(conf.device_id) is None:
            self.stdout.write(
                self.style.WARNING(f"SMSTUNNEL_DEVICE_ID {conf.device_id!r} is not one of this account's phones.")
            )

        to = (options.get("to") or "").strip()
        if not to:
            return
        try:
            result = send_sms(to, options["message"])
        except (SmsTunnelError, ValueError) as error:
            raise CommandError(f"The test text was not sent: {error}") from error
        self.stdout.write(self.style.SUCCESS(f"Test text accepted, message id {result.message_id}."))
        if result.queued:
            self.stdout.write(
                self.style.WARNING("The phone is offline: the text waits and goes out as soon as it is back online.")
            )
