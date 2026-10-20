from urllib.parse import urlencode

from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import resolve, Resolver404, reverse
from django.utils.cache import patch_cache_control

from .permissions import assigned_roles, is_ai_user


ACTIVE_ROLE_SESSION_KEY = "outreach_active_role"


class ActiveRoleMiddleware:
    """Require an explicit session role before any authenticated browser work."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            roles = assigned_roles(request.user)
            request.user._assigned_roles = roles
            selected = request.session.get(ACTIVE_ROLE_SESSION_KEY)
            if selected not in roles:
                request.session.pop(ACTIVE_ROLE_SESSION_KEY, None)
                selected = None
            request.user._active_role = roles[0] if len(roles) == 1 else selected
            # Allow only session actions and the user's own role-scoped API
            # credentials. A URL prefix must not exempt account-editing routes.
            try:
                route_name = resolve(request.path_info).url_name
            except Resolver404:
                route_name = None
            read_only_exempt = route_name in {
                "choose_role",
                "account_logout",
                "api_access",
                "api_access_token_revoke",
            }
            if (
                is_ai_user(request.user)
                and request.method not in {"GET", "HEAD", "OPTIONS"}
                and not read_only_exempt
            ):
                response = HttpResponseForbidden("AI user access is read-only.")
                patch_cache_control(response, no_store=True, private=True)
                return response
            exempt = (
                request.path in {reverse("choose_role"), reverse("login"), reverse("health")}
                or request.path.startswith(("/accounts/", "/api/", "/static/"))
            )
            if not request.user._active_role and not exempt:
                destination = reverse("choose_role")
                if request.method == "GET":
                    destination += "?" + urlencode({"next": request.get_full_path()})
                response = redirect(destination)
                patch_cache_control(response, no_store=True, private=True)
                return response
        response = self.get_response(request)
        if request.user.is_authenticated and response.get("Content-Type", "").startswith("text/html"):
            patch_cache_control(response, no_store=True, private=True)
        return response
