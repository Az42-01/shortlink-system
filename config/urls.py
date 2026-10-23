"""URL configuration for the ShortLinkSystem project."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("shortener.urls")),
]
