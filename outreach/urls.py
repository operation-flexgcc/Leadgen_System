from django.urls import path

from . import api, views


urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("companies/export/<str:file_format>/", views.company_export, name="company_export"),
    path("api-access/", api.api_access, name="api_access"),
    path("api/v1/token/refresh/", api.token_refresh, name="api_token_refresh"),
    path("api/v1/companies/<uuid:company_id>/", api.company_detail, name="api_company_detail"),
    path("api/v1/companies/<uuid:company_id>/claim/", api.company_claim, name="api_company_claim"),
    path("prospects/new/", views.prospect_create, name="prospect_create"),
    path("prospects/<int:pk>/", views.prospect_detail, name="prospect_detail"),
    path("prospects/<int:pk>/edit/", views.prospect_update, name="prospect_update"),
    path("prospects/<int:pk>/claim/", views.prospect_claim, name="prospect_claim"),
    path("prospects/<int:pk>/outreaches/add/", views.outreach_add, name="outreach_add"),
    path("outreaches/<int:pk>/edit/", views.outreach_update, name="outreach_update"),
    path("system/users/", views.user_list, name="user_list"),
    path("system/users/new/", views.user_create, name="user_create"),
    path("system/users/<int:pk>/edit/", views.user_update, name="user_update"),
    path("system/users/<int:pk>/delete/", views.user_delete, name="user_delete"),
    path("system/users/<int:pk>/restore/", views.user_restore, name="user_restore"),
]
