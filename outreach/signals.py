from django.contrib.auth import get_user_model
from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=get_user_model())
def ensure_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)


@receiver(user_logged_in)
def reset_session_role(sender, request, user, **kwargs):
    # A new login always prompts multi-role users, even if a previous login
    # selected a role. This applies to Google and Django authentication alike.
    if request is not None:
        from .middleware import ACTIVE_ROLE_SESSION_KEY

        request.session.pop(ACTIVE_ROLE_SESSION_KEY, None)
