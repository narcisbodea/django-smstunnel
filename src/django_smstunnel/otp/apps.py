from django.apps import AppConfig


class SmsTunnelOtpConfig(AppConfig):
    name = "django_smstunnel.otp"
    label = "smstunnel_otp"
    verbose_name = "SMSTunnel text message codes"
    default_auto_field = "django.db.models.BigAutoField"
