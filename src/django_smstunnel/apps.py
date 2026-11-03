from django.apps import AppConfig


class SmsTunnelConfig(AppConfig):
    name = "django_smstunnel"
    verbose_name = "SMSTunnel"

    def ready(self) -> None:
        from . import checks  # noqa: F401  (registers the system checks)
