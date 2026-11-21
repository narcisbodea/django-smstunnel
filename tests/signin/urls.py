from __future__ import annotations

from importlib.util import find_spec

from django.http import HttpRequest, HttpResponse
from django.urls import URLPattern, URLResolver, include, path


def signed_in(request: HttpRequest) -> HttpResponse:
    return HttpResponse(f"signed in: {request.user.is_authenticated}")


urlpatterns: list[URLPattern | URLResolver] = [path("signed-in/", signed_in)]

if find_spec("allauth"):
    urlpatterns.append(path("accounts/", include("allauth.urls")))
if find_spec("phone_verify"):
    urlpatterns.append(path("api/", include("phone_verify.urls")))
