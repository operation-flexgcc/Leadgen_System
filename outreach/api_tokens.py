import hashlib
import secrets

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import ApiAccessToken, ApiRefreshToken


API_ACCESS_TOKEN_PREFIX = "fgc_pat_"


class ApiTokenError(Exception):
    pass


def _token_hash(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_access_token(user):
    """Create a persistent credential while storing only its one-way hash."""

    raw_token = f"{API_ACCESS_TOKEN_PREFIX}{secrets.token_urlsafe(48)}"
    ApiAccessToken.objects.create(
        user=user,
        token_hash=_token_hash(raw_token),
        token_prefix=raw_token[:16],
    )
    return raw_token


def issue_token_pair(user):
    """Preserve the existing helper contract while issuing no refresh token."""

    return {
        "access_token": issue_access_token(user),
        "token_type": "Bearer",
        "expires_in": None,
    }


def _authenticate_persistent_access_token(raw_token):
    try:
        stored_token = (
            ApiAccessToken.objects.select_related("user", "user__profile")
            .get(token_hash=_token_hash(raw_token), revoked_at__isnull=True)
        )
    except ApiAccessToken.DoesNotExist as error:
        raise ApiTokenError("The access token is invalid or has been revoked.") from error

    if not stored_token.user.is_active:
        raise ApiTokenError("The token user is unavailable or inactive.")

    now = timezone.now()
    updated = ApiAccessToken.objects.filter(
        pk=stored_token.pk,
        revoked_at__isnull=True,
    ).update(last_used_at=now)
    if not updated:
        raise ApiTokenError("The access token is invalid or has been revoked.")
    return stored_token.user


def _authenticate_legacy_access_token(raw_token):
    """Accept pre-migration JWTs until their original expiry time."""

    try:
        payload = jwt.decode(
            raw_token,
            settings.API_TOKEN_SIGNING_KEY,
            algorithms=["HS256"],
            audience=settings.API_TOKEN_AUDIENCE,
            issuer=settings.API_TOKEN_ISSUER,
            options={"require": ["exp", "iat", "iss", "aud", "sub", "type", "jti"]},
        )
    except jwt.PyJWTError as error:
        raise ApiTokenError("The access token is invalid or has expired.") from error
    if payload.get("type") != "access":
        raise ApiTokenError("The supplied token is not an access token.")
    try:
        return get_user_model().objects.select_related("profile").get(
            pk=int(payload["sub"]),
            is_active=True,
        )
    except (TypeError, ValueError, get_user_model().DoesNotExist) as error:
        raise ApiTokenError("The token user is unavailable or inactive.") from error


def authenticate_access_token(raw_token):
    if raw_token.startswith(API_ACCESS_TOKEN_PREFIX):
        return _authenticate_persistent_access_token(raw_token)
    return _authenticate_legacy_access_token(raw_token)


@transaction.atomic
def rotate_refresh_token(raw_token):
    """Exchange one old refresh token for one persistent access token."""

    now = timezone.now()
    try:
        stored_token = (
            ApiRefreshToken.objects.select_for_update()
            .select_related("user")
            .get(token_hash=_token_hash(raw_token))
        )
    except ApiRefreshToken.DoesNotExist as error:
        raise ApiTokenError("The refresh token is invalid or has expired.") from error

    if (
        stored_token.revoked_at is not None
        or stored_token.expires_at <= now
        or not stored_token.user.is_active
    ):
        raise ApiTokenError("The refresh token is invalid or has expired.")

    stored_token.last_used_at = now
    stored_token.revoked_at = now
    stored_token.save(update_fields=["last_used_at", "revoked_at"])
    return issue_token_pair(stored_token.user)
