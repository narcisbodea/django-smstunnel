"""The adapter a project writes for allauth's phone sign-in: our mixin and the four storage methods."""

from __future__ import annotations

from typing import Any

from allauth.account.adapter import DefaultAccountAdapter

from django_smstunnel.allauth import SmsTunnelAccountAdapterMixin

from .phoneapp.models import UserPhone


class PhoneAdapter(SmsTunnelAccountAdapterMixin, DefaultAccountAdapter):  # type: ignore[misc,unused-ignore]
    def set_phone(self, user: Any, phone: str, verified: bool) -> None:
        UserPhone.objects.update_or_create(user=user, defaults={"number": phone, "verified": verified})

    def get_phone(self, user: Any) -> tuple[str, bool] | None:
        stored = UserPhone.objects.filter(user=user).first()
        return (stored.number, stored.verified) if stored else None

    def set_phone_verified(self, user: Any, phone: str) -> None:
        UserPhone.objects.filter(user=user, number=phone).update(verified=True)

    def get_user_by_phone(self, phone: str) -> Any:
        stored = UserPhone.objects.filter(number=phone).select_related("user").first()
        return stored.user if stored else None
