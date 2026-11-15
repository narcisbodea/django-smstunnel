# Changelog

All notable changes to `django-smstunnel` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-11-15

### Added

- **Scheduled texts.** `send_sms()` and `asend_sms()` take `send_at`: an aware `datetime` or an ISO 8601 string with
  an offset, 1 minute to 30 days ahead. They then return a `ScheduledText` (`id`, `send_at`, `status`, `replayed`)
  instead of a `SendResult`. Django's conventions apply: with `USE_TZ = True` a naive `datetime` raises `ValueError`
  before anything is sent; with `USE_TZ = False` it is local time, in the current time zone.
- `list_scheduled()` and `cancel_scheduled()`, with `alist_scheduled()` and `acancel_scheduled()`, for the texts the
  project's key scheduled; `get_client().get_scheduled(id)` reads one.
- `ScheduledText` and `ScheduledTexts`, re-exported from `smstunnel`.
- A send answered with SMSTunnel's first answer for its idempotency key has `result.replayed` set (from `smstunnel`
  1.2).

### Changed

- Requires `smstunnel` 1.2.
- `send_sms()` and `asend_sms()` are typed with `@overload`: without `send_at` they return a `SendResult`, as before.

## [1.0.0] - 2026-11-03

### Added

- `send_sms()` and `send_sign_in_code()`, with `asend_sms()` and `asend_sign_in_code()` for async code: one text to
  one number, from your own phone, configured by the `SMSTUNNEL_*` settings (`SMSTUNNEL_API_KEY`,
  `SMSTUNNEL_DEVICE_ID`, `SMSTUNNEL_DEFAULT_COUNTRY_CODE`, `SMSTUNNEL_BASE_URL`, `SMSTUNNEL_TIMEOUT`).
- `idempotency_key=` on every send, sent as the `Idempotency-Key` header: a retry with the same key within 24 hours
  gets the first answer instead of sending a second text. `is_retryable()` and `retry_after()` for background tasks.
- `get_client()`: the project's `smstunnel.Client` for statuses, phones and usage, rebuilt when a setting changes.
- `django_smstunnel.two_factor.SmsTunnel` for django-two-factor-auth (`TWO_FACTOR_SMS_GATEWAY`): sign-in codes from
  the overridable template `django_smstunnel/two_factor/sms_message.txt`, translated with django-two-factor-auth's
  catalogs.
- System checks `smstunnel.W001` (no API key), `smstunnel.E001` (a setting SMSTunnel can't use), `smstunnel.W002`
  (phone calls pointed at SMSTunnel) and `smstunnel.W003` (an `http://` address outside your own machine).
- `manage.py smstunnel_test`: checks the key, lists your phones and, with `--to`, sends one test text.
- The exceptions and result types of [`smstunnel`](https://pypi.org/project/smstunnel/), re-exported.
