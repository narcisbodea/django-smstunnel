from typing import Any, ClassVar

from django.conf import settings
from django.db import models


class UserPhone(models.Model):
    """Where this test project keeps a user's number: what a real project writes for allauth."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="phone")
    number = models.CharField(max_length=16, unique=True)
    verified = models.BooleanField(default=False)

    objects: ClassVar[Any] = models.Manager()

    def __str__(self) -> str:
        return f"Phone {self.pk}"
