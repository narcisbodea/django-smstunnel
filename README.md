# SMSTunnel for Django

`django-smstunnel`: send texts from your Django app through **your own Android phone**, from **your own number**, with [SMSTunnel](https://smstunnel.io). It also sends django-two-factor-auth's sign-in codes.

Your app hands each text to your SMSTunnel account and your phone sends it from its own SIM, so recipients see a number they know and can reply to. The phone is linked to your account when you sign in to the SMSTunnel app, and you can cut your app off at any time by deleting its API key in your dashboard.

Your phone needs the SMSTunnel app from Google Play:

<a href="https://play.google.com/store/apps/details?id=io.smstunnel.app"><img src="https://smstunnel.io/google-play-badge.png" alt="Get it on Google Play" height="56"></a>

SMSTunnel for Django is made by NARBOWEB SRL. It is not affiliated with, endorsed by, or sponsored by the Django Software Foundation or the django-two-factor-auth project (Jazzband). Django is a registered trademark of the Django Software Foundation.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Get your key](#get-your-key)
- [Setup](#setup)
- [Send a text](#send-a-text)
- [Scheduled texts](#scheduled-texts)
- [Settings](#settings)
- [Sign-in codes with django-two-factor-auth](#sign-in-codes-with-django-two-factor-auth)
- [Errors](#errors)
- [Background tasks and retries](#background-tasks-and-retries)
- [Status, phones and usage](#status-phones-and-usage)
- [Notes and limits](#notes-and-limits)
- [Build it with an AI assistant](#build-it-with-an-ai-assistant)
- [Responsible use](#responsible-use)

## Requirements

- Python 3.10 or newer
- Django 5.2, 6.0 or 6.1
- One dependency besides Django: [`smstunnel`](https://pypi.org/project/smstunnel/), the SMSTunnel client for Python, which needs nothing else.

## Installation

```bash
pip install django-smstunnel
```

For django-two-factor-auth sign-in codes, install it with the extra (it brings django-two-factor-auth and a phone number library):

```bash
pip install "django-smstunnel[two-factor]"
```

## Get your key

1. Install the [SMSTunnel app from Google Play](https://play.google.com/store/apps/details?id=io.smstunnel.app) on your Android phone and sign in. Sign in at [smstunnel.io](https://smstunnel.io) on your computer with the same account: the phone appears under **Phones** (you can also link it with the QR code from **Phones → Add phone**).
2. In your [SMSTunnel dashboard](https://dashboard.smstunnel.io/apps), open **Apps**.
3. Click **Add an app** and copy its key. The key can send texts and look up whether they went out (`sms:send` and `sms:read`).

Keep the key out of your code: put it in an environment variable such as `SMSTUNNEL_API_KEY`.

## Setup

```python
# settings.py
import os

INSTALLED_APPS = [
    # ...
    "django_smstunnel",
]

SMSTUNNEL_API_KEY = os.environ.get("SMSTUNNEL_API_KEY", "")
```

Adding the app to `INSTALLED_APPS` turns on the system checks and the `smstunnel_test` command. `python manage.py check` warns when the key is missing or a setting has a value SMSTunnel can't use.

Check the setup from the command line. Without `--to` it only lists your phones; with it, it sends one test text:

```bash
python manage.py smstunnel_test
python manage.py smstunnel_test --to +14155550123
```

```text
The API key works (https://smstunnel.io).
  Pixel 8 (6740f1c2a8b9c0d1e2f3a4b5): online
Test text accepted, message id 6740f2d0a8b9c0d1e2f3a4c7.
```

## Send a text

```python
from django_smstunnel import send_sms

result = send_sms("+14155550123", "Your order #1042 has shipped.")

result.message_id  # keep it to look up the status later
result.queued      # True when the phone was offline: it sends the text as soon as it is back
```

All the arguments:

```python
send_sms(
    "+14155550123",                       # one number, in international format
    "See you at 10:30.",                  # up to 1600 characters
    device_id=None,                       # a phone's ID; default SMSTUNNEL_DEVICE_ID, else SMSTunnel picks one of yours
    default_country_code=None,            # e.g. "+40": completes numbers written without + or 00
    idempotency_key="booking-881-remind", # optional: a retry with the same key never sends a second text
)
```

One call sends one text to one number. To text several people, call `send_sms()` once for each.

Send after the data is saved, so a failed save never texts anyone:

```python
from django.db import transaction

transaction.on_commit(lambda: send_sms(order.phone, f"Your order #{order.pk} has shipped."))
```

In async views use `asend_sms()` and `asend_sign_in_code()`, the same functions as coroutines.

### Sign-in and verification codes

```python
from django_smstunnel import send_sign_in_code

send_sign_in_code("+14155550123", "Your sign-in code is 482913")
```

The text must contain the code, 4 to 10 digits standing on their own (for example `482913` or `123-456`); SMSTunnel refuses it otherwise. Codes count against a separate quota, and the text can be up to 320 characters. Use it for sign-in and verification codes only.

## Scheduled texts

Give `send_sms()` a `send_at`, 1 minute to 30 days ahead, and SMSTunnel hands the text to your phone at that time. It then returns a `ScheduledText` instead of a `SendResult`:

```python
from datetime import timedelta

from django_smstunnel import cancel_scheduled, send_sms

reminder = send_sms(
    booking.phone,
    f"Reminder: your appointment is tomorrow at {booking.starts_at:%H:%M}.",
    send_at=booking.starts_at - timedelta(days=1),  # aware, as Django's datetimes are with USE_TZ = True
    idempotency_key=f"booking-{booking.pk}-reminder",
)
booking.reminder_id = reminder.id  # keep it to cancel the text
booking.save(update_fields=["reminder_id"])

cancel_scheduled(booking.reminder_id)  # the booking was canceled while the text still waits
```

- `send_at` is an aware `datetime` or an ISO 8601 string with an offset. With `USE_TZ = True` (Django's default), a naive `datetime` raises `ValueError` before anything is sent: use `timezone.now() + timedelta(...)`, a `DateTimeField`'s value or `timezone.make_aware()`. With `USE_TZ = False`, a naive `datetime` is local time, in the current time zone, as Django takes it.
- At that time SMSTunnel sends the text as if your app sent it then, with the same key: it picks the phone then (unless you named one), and the text counts toward that day's allowance. If the phone is offline, the text waits for it up to 3 days.
- `list_scheduled()` (`"waiting"`, soonest first, or `"done"`) and `cancel_scheduled(id)` work on the texts your project's key scheduled; `alist_scheduled()`, `acancel_scheduled()` and `asend_sms(..., send_at=...)` are for async code, and `get_client().get_scheduled(id)` reads one (`.message_id` once it went out).
- Sign-in codes can't be scheduled.
- Refusals: `ApiError` 400 with `error_code` `SCHEDULE_TOO_SOON` or `SCHEDULE_TOO_FAR`, 409 `SCHEDULE_NOT_EDITABLE` when the text to cancel already went out, and `PermissionDeniedError` 403 `SCHEDULE_LIMIT_REACHED` when as many texts wait as your plan allows (10 on Free up to 1000 on Enterprise). `is_retryable()` is `False` for all of them.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `SMSTUNNEL_API_KEY` | (required) | The key from **Apps → Add an app**. Read it from the environment. |
| `SMSTUNNEL_DEVICE_ID` | `None` | The phone that sends when a call doesn't name one. Copy the ID from **Phones** in your dashboard. Without it, SMSTunnel picks one of your phones. |
| `SMSTUNNEL_DEFAULT_COUNTRY_CODE` | `None` | Completes numbers written without `+` or `00`, e.g. `"+40"` (`"40"` and `"0040"` work too). It always wins over the phone's own country. |
| `SMSTUNNEL_BASE_URL` | `"https://smstunnel.io"` | Change it only for a test server. |
| `SMSTUNNEL_TIMEOUT` | `30` | Seconds for each request. |

Changing a setting in a test with `override_settings` takes effect at once.

## Sign-in codes with django-two-factor-auth

[django-two-factor-auth](https://github.com/jazzband/django-two-factor-auth) can send sign-in codes by text; SMSTunnel sends them from your own phone. Set up django-two-factor-auth as its [installation guide](https://django-two-factor-auth.readthedocs.io/en/stable/installation.html) says, then add its phone number plugin and point it at SMSTunnel:

```python
# settings.py
INSTALLED_APPS = [
    # ... django_otp, its plugins and two_factor, as django-two-factor-auth's guide says
    "two_factor.plugins.phonenumber",  # the "Text message" method
    "django_smstunnel",
]

TWO_FACTOR_SMS_GATEWAY = "django_smstunnel.two_factor.SmsTunnel"
```

That is all: when a user picks **Text message** in django-two-factor-auth's setup, and at every sign-in, the code goes out through your phone. The number comes from the user's phone device, and the text from the template `django_smstunnel/two_factor/sms_message.txt`:

```text
Your OTP token is 482913
```

It is translated with django-two-factor-auth's own translations, in the user's language. To change the words, put your own `templates/django_smstunnel/two_factor/sms_message.txt` in your project. Keep the code on its own; for example, with the line browsers use to fill in the code automatically:

```django
{% load i18n %}{% autoescape off %}{% blocktrans trimmed %}Your OTP token is {{ token }}{% endblocktrans %}

@example.com #{{ token }}{% endautoescape %}
```

Phone calls are not supported: leave `TWO_FACTOR_CALL_GATEWAY` unset (the system check warns when it points at SMSTunnel). The `SMSTUNNEL_DEVICE_ID` and `SMSTUNNEL_DEFAULT_COUNTRY_CODE` settings apply to these codes too.

Every request for a code sends a text. If a double click should not send the same code twice, subclass `SmsTunnel`, return a key from `idempotency_key(self, device, token)` (for example `f"sign-in-{device.persistent_id}-{token}"`) and point `TWO_FACTOR_SMS_GATEWAY` at your class.

## Errors

Every function raises a subclass of `django_smstunnel.SmsTunnelError` when the text was not accepted:

| Exception | When |
| --- | --- |
| `AuthenticationError` (401) | The API key is wrong, was deleted, or its account is suspended. |
| `PermissionDeniedError` (403) | The key lacks a permission (`error_code` is `PERMISSION_REQUIRED`, `permission` names it), or the plan does not include the feature. |
| `RateLimitError` (429) | Too many requests. `retry_after` gives the seconds to wait (SMSTunnel's `Retry-After` header). |
| `ApiError` | Any other error answer: 400 (a number it can't read, a sign-in text without a code), 404, 409/422 (an idempotency key that is still in use or was used for another text), 5xx. The three above extend it. `status_code`, `error_code` and `message` say what happened. |
| `TransportError` | SMSTunnel could not be reached (DNS, TLS, timeout). |

A missing `SMSTUNNEL_API_KEY` raises `ImproperlyConfigured`, and an empty number or text raises `ValueError`, before anything is sent. An offline phone is not an error: the text waits on your account (`result.queued` is `True`) and goes out when the phone is back online.

```python
import logging

from django_smstunnel import ApiError, RateLimitError, TransportError, send_sms

log = logging.getLogger(__name__)

try:
    send_sms(customer.phone, "Your table is ready.")
except RateLimitError as error:
    log.warning("SMSTunnel: too many texts, try again in %s s", error.retry_after)
except ApiError as error:
    log.warning("SMSTunnel %s %s: %s", error.status_code, error.error_code or "-", error.message)
except TransportError:
    log.warning("SMSTunnel could not be reached")
```

## Background tasks and retries

`send_sms()` never retries on its own: after a timeout or a server error, the first attempt may already have sent the text. To retry safely, give the text an `idempotency_key` of your own, the same on every attempt. SMSTunnel remembers it for 24 hours and answers a repeat with the first result instead of sending a second text.

With Celery:

```python
# shop/tasks.py
from celery import shared_task

from django_smstunnel import SmsTunnelError, is_retryable, retry_after, send_sms

from .models import Order


@shared_task(bind=True, max_retries=5)
def text_order_shipped(self, order_id: int) -> None:
    order = Order.objects.get(pk=order_id)
    try:
        send_sms(
            order.phone,
            f"Your order #{order.pk} has shipped.",
            idempotency_key=f"order-{order.pk}-shipped",  # the same key on every attempt
        )
    except SmsTunnelError as error:
        if is_retryable(error):  # 429, 5xx, a request still running, or no answer
            raise self.retry(exc=error, countdown=retry_after(error) or 60) from error
        raise  # 400, 401, 403: fix the number, the key or the plan instead
```

```python
transaction.on_commit(lambda: text_order_shipped.delay(order.pk))
```

The same applies to RQ, Dramatiq, Huey or Django's task framework: one key per text, kept across retries. A key is 1 to 255 printable ASCII characters; a UUID stored with the record works too.

## Status, phones and usage

`get_client()` returns the project's [`smstunnel.Client`](https://pypi.org/project/smstunnel/), made from the settings, for everything else:

```python
from django_smstunnel import get_client

client = get_client()

status = client.get_message_status(result.message_id)
status.status       # "pending", "sent", "delivered" or "failed"
status.is_final     # True once it will not change any more

for phone in client.get_devices():
    print(phone.name, "online" if phone.is_online else "offline")

client.get_usage().remaining  # today's quota left
```

Reading the texts your phone received needs a key you allowed to (**Can also read received texts**); see `client.get_received_messages()` in the [smstunnel README](https://pypi.org/project/smstunnel/). You can also have them forwarded to a URL of your app: **Apps → the key → Forward received texts**.

### In your own tests

Patch the function where your code uses it, so tests never send a text:

```python
from unittest import mock


def test_shipping_texts_the_customer(client, order):
    with mock.patch("shop.views.send_sms") as send_sms:
        client.post(f"/orders/{order.pk}/ship/")

    send_sms.assert_called_once_with(order.phone, f"Your order #{order.pk} has shipped.")
```

## Notes and limits

- **Rate limit**: 30 requests a minute per API key. A 429 carries `Retry-After`.
- **Length**: up to 1600 characters (10 joined SMS); sign-in codes up to 320.
- **Numbers**: use the international format (`+14155550123`). For local numbers, set `SMSTUNNEL_DEFAULT_COUNTRY_CODE` or pass `default_country_code`.
- **Offline phone**: the text waits on your account and goes out when the phone is back online.
- **Plans**: the free plan has a daily limit; see [smstunnel.io/pricing](https://smstunnel.io/pricing/).
- **Documentation**: [smstunnel.io/docs](https://smstunnel.io/docs/). Keys: [dashboard.smstunnel.io/apps](https://dashboard.smstunnel.io/apps).

## Build it with an AI assistant

Paste this into Claude, ChatGPT, Cursor or Copilot, and replace the part in brackets. Never paste your real API key into a prompt: the prompt doesn't need it.

```text
I want my Django app to send texts with SMSTunnel. SMSTunnel sends each text from my own Android phone, from my own number: the phone runs the SMSTunnel app and is linked to my SMSTunnel account.

Use the django-smstunnel package: `pip install django-smstunnel`, add "django_smstunnel" to INSTALLED_APPS and set SMSTUNNEL_API_KEY = os.environ.get("SMSTUNNEL_API_KEY", "") in settings.py. Then `from django_smstunnel import send_sms`.

API facts:
- send_sms(to, message, device_id=None, default_country_code=None, idempotency_key=None) calls POST https://smstunnel.io/api/v1/sms/send with the header X-API-Key and the JSON body {"to", "message", "deviceId"?, "defaultCountryCode"?}. It returns result.message_id and result.queued.
- One recipient per call. To text several people, call it once per number.
- Scheduled texts: send_sms(..., send_at=booking.starts_at - timedelta(days=1)), 1 minute to 30 days ahead (an aware datetime or an ISO 8601 string with an offset; with USE_TZ = True a naive datetime raises ValueError), returns a ScheduledText (id, send_at, status) instead of a SendResult; cancel_scheduled(id) and list_scheduled() manage them. Not for sign-in codes.
- send_sign_in_code(...) (POST /api/v1/sms/send-2fa) is only for sign-in codes: the text must contain a code of 4 to 10 digits standing on its own (e.g. 482913 or 123-456), at most 320 characters; it uses a separate quota. For django-two-factor-auth, set TWO_FACTOR_SMS_GATEWAY = "django_smstunnel.two_factor.SmsTunnel".
- Numbers in international format (+14155550123). default_country_code or the setting SMSTUNNEL_DEFAULT_COUNTRY_CODE (e.g. "+40") completes numbers written without + or 00.
- Limits: 30 requests a minute per API key, 1600 characters a text.
- idempotency_key is sent as the Idempotency-Key header: a retry with the same key within 24 hours gets the first answer instead of sending a second text. Never retry a send after a timeout, a 5xx or a 429 unless it carries a key.
- Errors (all subclasses of SmsTunnelError): 401 AuthenticationError (bad key), 403 PermissionDeniedError (missing permission or plan; error_code such as PERMISSION_REQUIRED), 429 RateLimitError (retry_after seconds from the Retry-After header), other 4xx/5xx ApiError (status_code, error_code, message), TransportError when SMSTunnel can't be reached. is_retryable(error) and retry_after(error) say whether and when a retry may work.
- If the phone is offline, the text waits on the account (result.queued is True) and goes out when the phone is back.

What I want: [describe what should send a text, to whom, and when]

Rules:
- Read the API key from an environment variable or the platform's secret store. Never put it in source code, in the repository or in client-side code.
- Validate phone numbers before sending, and handle every error above: log it clearly, don't retry 400/401/403, and retry 429/5xx/timeouts only with an idempotency key, after the wait SMSTunnel asks for.
- Send after the database transaction commits (transaction.on_commit) or from a background task.
- Only send texts people expect (alerts to myself, reminders, sign-in codes, order updates).
```

## Responsible use

Send only texts that people expect from you: alerts to yourself, appointment reminders, sign-in codes, order updates. Unsolicited messages from a personal SIM break most carriers' terms of service. Please make sure your usage complies with your carrier's terms and with local regulations (in the EU, that includes GDPR and ePrivacy rules on electronic communications).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . --group dev        # pip 25.1 or newer
pytest                              # unit tests (HTTP is faked)
ruff check . && ruff format --check .
pip install --group typing && mypy  # strict, with the Django stubs (Python 3.11 or newer)
```

The end-to-end tests run only against a test server, never production. They read the server address and a test key from a JSON file (`{"serverUrl": "http://127.0.0.1:3141", "apiKey": "...", "deviceId": "..."}`) and, when `SMSTUNNEL_E2E_MONGO_URL` is set, check each text in the server's database:

```bash
pip install --group e2e
SMSTUNNEL_E2E_KEY_FILE=/path/to/test-key.json SMSTUNNEL_E2E_MONGO_URL=mongodb://127.0.0.1:27018/smstunnel_test pytest tests/e2e
```

## Changelog

See [CHANGELOG.md](https://github.com/narcisbodea/django-smstunnel/blob/main/CHANGELOG.md).

## License

MIT, © NARBOWEB SRL. See [LICENSE](https://github.com/narcisbodea/django-smstunnel/blob/main/LICENSE).
