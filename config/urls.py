from django.contrib import admin
from django.urls import include, path

from outreach import views


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("login/", views.login_landing, name="login"),
    path("health/", views.health, name="health"),
    path("", include("outreach.urls")),
]
