from __future__ import annotations

from django.contrib import admin

from .models import SmsTunnelDevice


@admin.register(SmsTunnelDevice)
class SmsTunnelDeviceAdmin(admin.ModelAdmin):  # type: ignore[type-arg,unused-ignore]
    """The current code and its expiry stay out of the admin."""

    list_display = ("user", "name", "confirmed", "created_at", "last_used_at")
    list_filter = ("confirmed",)
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "last_used_at")
    fields = ("user", "name", "confirmed", "number", "created_at", "last_used_at")
