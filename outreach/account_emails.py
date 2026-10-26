"""Explicit account email sending; does not create users or change passwords."""
from allauth.account import app_settings
from allauth.account.adapter import get_adapter
from allauth.account.utils import user_pk_to_url_str

from .email_backends import EmailDeliveryError, require_email_configuration


def send_password_setup_email(user):
    require_email_configuration()
    if not user.is_active or not user.email:
        raise EmailDeliveryError("An active account with an email address is required.")
    adapter = get_adapter()
    token = app_settings.PASSWORD_RESET_TOKEN_GENERATOR().make_token(user)
    key = f"{user_pk_to_url_str(user)}-{token}"
    message = adapter.render_mail("account/email/password_reset_key", user.email, {
        "user": user, "password_reset_url": adapter.get_reset_password_from_key_url(key),
    })
    if message.send() != 1:
        raise EmailDeliveryError("The email provider did not confirm acceptance.")
