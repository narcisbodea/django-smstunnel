"""Django settings for the test suite. django-two-factor-auth is optional: without it, its tests are skipped."""

from importlib.util import find_spec

SECRET_KEY = "tests-only"
USE_TZ = True
USE_I18N = True
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

HAS_TWO_FACTOR = find_spec("two_factor") is not None and find_spec("phonenumber_field") is not None

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
]
if HAS_TWO_FACTOR:
    INSTALLED_APPS += [
        "django_otp",
        "two_factor",
        "two_factor.plugins.phonenumber",
    ]
INSTALLED_APPS += ["django_smstunnel"]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True}]

SMSTUNNEL_API_KEY = "sk_test_unit"
SMSTUNNEL_BASE_URL = "https://smstunnel.test"

TWO_FACTOR_PATCH_ADMIN = False
TWO_FACTOR_SMS_GATEWAY = "django_smstunnel.two_factor.SmsTunnel"
