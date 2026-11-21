"""Settings for tests/signin: the real django-allauth, django-otp and django-phone-verify.

    pytest --ds=tests.settings_signin tests/signin

Each host is added only when it is installed; its tests are skipped otherwise. django-phone-verify can't share an
environment with django-two-factor-auth (they need different major versions of phonenumbers).
"""

from importlib.util import find_spec

from .settings import *  # noqa: F403
from .settings import INSTALLED_APPS

HAS_ALLAUTH = find_spec("allauth") is not None
HAS_OTP = find_spec("django_otp") is not None
HAS_PHONE_VERIFY = find_spec("phone_verify") is not None

INSTALLED_APPS = [
    *INSTALLED_APPS,
    "django.contrib.sessions",
    "django.contrib.messages",
    "tests.signin.phoneapp",
]
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_smstunnel.signin.ClientIPMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
ROOT_URLCONF = "tests.signin.urls"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "signin-tests"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
SMSTUNNEL_SIGNIN_BACKGROUND = False
# django-phone-verify signs its session tokens with this key, and PyJWT warns about a short one.
SECRET_KEY = "tests-only-a-key-long-enough-for-hs256-signatures"

if HAS_OTP:
    if "django_otp" not in INSTALLED_APPS:
        INSTALLED_APPS.append("django_otp")
    INSTALLED_APPS.append("django_smstunnel.otp")

if HAS_ALLAUTH:
    INSTALLED_APPS += ["allauth", "allauth.account"]
    MIDDLEWARE.append("allauth.account.middleware.AccountMiddleware")
    AUTHENTICATION_BACKENDS = [
        "django.contrib.auth.backends.ModelBackend",
        "allauth.account.auth_backends.AuthenticationBackend",
    ]
    ACCOUNT_ADAPTER = "tests.signin.adapters.PhoneAdapter"
    ACCOUNT_LOGIN_METHODS = {"phone"}
    ACCOUNT_SIGNUP_FIELDS = ["phone*", "username*", "password1*", "password2*"]
    ACCOUNT_LOGIN_BY_CODE_ENABLED = True
    ACCOUNT_EMAIL_VERIFICATION = "none"
    ACCOUNT_PHONE_VERIFICATION_ENABLED = True
    # The tests sign in many times from one address.
    ACCOUNT_RATE_LIMITS = False
    LOGIN_REDIRECT_URL = "/signed-in/"

if HAS_PHONE_VERIFY:
    INSTALLED_APPS += ["rest_framework", "phone_verify"]
    PHONE_VERIFICATION = {
        "BACKEND": "django_smstunnel.phone_verify.SmsTunnelBackend",
        "OPTIONS": {},
        "TOKEN_LENGTH": 6,
        "MESSAGE": "Your {app} code is {security_code}",
        "APP_NAME": "Example",
        "SECURITY_CODE_EXPIRATION_SECONDS": 600,
        "VERIFY_SECURITY_CODE_ONLY_ONCE": True,
    }
