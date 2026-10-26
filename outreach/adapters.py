from allauth.core.exceptions import ImmediateHttpResponse
from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.models import EmailAddress
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib.auth import get_user_model
from django.shortcuts import render

from .models import Profile


class FlexGCCAccountAdapter(DefaultAccountAdapter):
    def is_open_for_signup(self, request):
        # Password login is for admin-provisioned accounts, never public signup.
        return False


class FlexGCCSocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        email = (sociallogin.user.email or "").strip().lower()
        # Preserve approved Google bootstrap. pre_social_login still enforces
        # disabled accounts, preprovisioning and the configured domain policy.
        return (not settings.REQUIRE_PREPROVISIONED_USERS
                or email in settings.SYSTEM_ADMIN_EMAILS or email in settings.MANAGER_EMAILS)

    def pre_social_login(self, request, sociallogin):
        super().pre_social_login(request, sociallogin)
        email = (sociallogin.user.email or sociallogin.account.extra_data.get("email", "")).strip().lower()
        if not email:
            raise ImmediateHttpResponse(
                render(request, "account/access_denied.html", {"reason": "Your Google account did not provide an email address."}, status=403)
            )
        existing_user = get_user_model().objects.filter(email__iexact=email).first()
        bootstrap_email = email in settings.SYSTEM_ADMIN_EMAILS or email in settings.MANAGER_EMAILS
        if existing_user and not existing_user.is_active:
            raise ImmediateHttpResponse(
                render(
                    request,
                    "account/access_denied.html",
                    {"reason": "Access for this account has been disabled. Contact a system administrator."},
                    status=403,
                )
            )
        if settings.REQUIRE_PREPROVISIONED_USERS and not existing_user and not bootstrap_email:
            raise ImmediateHttpResponse(
                render(
                    request,
                    "account/access_denied.html",
                    {"reason": "A system administrator must add your name, email, and user class before your first sign-in."},
                    status=403,
                )
            )
        # Exact-email approval is more specific than the optional domain gate.
        # Apply the domain allowlist only to accounts that were not explicitly
        # preprovisioned or configured as bootstrap users.
        if not existing_user and not bootstrap_email and settings.GOOGLE_ALLOWED_DOMAINS:
            domain = email.rsplit("@", 1)[-1]
            if domain not in settings.GOOGLE_ALLOWED_DOMAINS:
                allowed = ", ".join(sorted(settings.GOOGLE_ALLOWED_DOMAINS))
                raise ImmediateHttpResponse(
                    render(
                        request,
                        "account/access_denied.html",
                        {"reason": f"Use an approved Google account. Allowed domains: {allowed}."},
                        status=403,
                    )
                )
        if (
            existing_user and sociallogin.is_existing
            and sociallogin.user.pk == existing_user.pk
            and sociallogin.account.provider == "google"
            and existing_user.profile.password_provisioned_by_admin
            and any(address.verified and address.email.lower() == email for address in sociallogin.email_addresses)
        ):
            # Google has now verified ownership of the exact admin-approved
            # identity. Preserve its deliberately provisioned local password
            # when allauth connects Google; never trust an unverified assertion
            # or bypass allauth's password wipe for unprovisioned accounts.
            EmailAddress.objects.filter(user=existing_user, email__iexact=email).update(verified=True)
        if existing_user and email in settings.SYSTEM_ADMIN_EMAILS:
            profile, _ = Profile.objects.get_or_create(user=existing_user)
            profile.set_roles([*profile.assigned_roles, Profile.Role.SYSTEM_ADMIN])
        elif existing_user and email in settings.MANAGER_EMAILS:
            profile, _ = Profile.objects.get_or_create(user=existing_user)
            profile.set_roles([*profile.assigned_roles, Profile.Role.MANAGER])

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        email = user.email.lower()
        profile, created = Profile.objects.get_or_create(user=user)
        if email in settings.SYSTEM_ADMIN_EMAILS:
            profile.role = Profile.Role.SYSTEM_ADMIN
            profile.save(update_fields=["role"])
        elif email in settings.MANAGER_EMAILS:
            profile.role = Profile.Role.MANAGER
            profile.save(update_fields=["role"])
        elif created:
            profile.role = Profile.Role.INTERN
            profile.save(update_fields=["role"])
        return user
