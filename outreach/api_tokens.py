import hashlib
import secrets
import uuid
from datetime import timedelta

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .models import ApiRefreshToken


class ApiTokenError(Exception):
    pass


def _refresh_hash(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _access_lifetime():
    return timedelta(minutes=settings.API_ACCESS_TOKEN_MINUTES)


def _refresh_lifetime():
    return timedelta(days=settings.API_REFRESH_TOKEN_DAYS)


def issue_access_token(user):
    now = timezone.now()
    expires_at = now + _access_lifetime()
    payload = {
        "iss": settings.API_TOKEN_ISSUER,
        "aud": settings.API_TOKEN_AUDIENCE,
        "sub": str(user.pk),
        "email": user.email,
        "type": "access",
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.API_TOKEN_SIGNING_KEY, algorithm="HS256")


def _issue_refresh_token(user):
    raw_token = secrets.token_urlsafe(48)
    ApiRefreshToken.objects.create(
        user=user,
        token_hash=_refresh_hash(raw_token),
        expires_at=timezone.now() + _refresh_lifetime(),
    )
    return raw_token


def issue_token_pair(user):
    return {
        "access_token": issue_access_token(user),
        "refresh_token": _issue_refresh_token(user),
        "token_type": "Bearer",
        "expires_in": int(_access_lifetime().total_seconds()),
    }


def authenticate_access_token(raw_token):
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


@transaction.atomic
def rotate_refresh_token(raw_token):
    now = timezone.now()
    try:
        stored_token = (
            ApiRefreshToken.objects.select_for_update()
            .select_related("user")
            .get(token_hash=_refresh_hash(raw_token))
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
