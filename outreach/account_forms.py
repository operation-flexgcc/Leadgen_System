import logging

from allauth.account.forms import ResetPasswordForm, ResetPasswordKeyForm, UserTokenForm
from allauth.account.models import EmailAddress
from django import forms
from django.db import transaction

from .email_backends import EmailDeliveryError, require_email_configuration

logger = logging.getLogger(__name__)


class PasswordResetForm(ResetPasswordForm):
    def clean(self):
        cleaned = super().clean()
        try:
            require_email_configuration()
        except EmailDeliveryError:
            # Same response for registered and unregistered addresses.
            raise forms.ValidationError("Email reset is temporarily unavailable. Please contact your system administrator.") from None
        return cleaned

    def save(self, request, **kwargs):
        try:
            return super().save(request, **kwargs)
        except EmailDeliveryError:
            # Public response must not reveal whether this account exists.
            logger.warning("Password-reset email delivery was not confirmed. Check SendGrid configuration and activity.")
            return self.cleaned_data["email"]


class ActiveUserTokenForm(UserTokenForm):
    def _get_user(self, uidb36):
        user = super()._get_user(uidb36)
        return user if user and user.is_active else None


class PasswordResetKeyForm(ResetPasswordKeyForm):
    @transaction.atomic
    def save(self):
        super().save()
        # A successful emailed-token reset proves control of this identity.
        EmailAddress.objects.filter(user=self.user, email__iexact=self.user.email).update(verified=True)
