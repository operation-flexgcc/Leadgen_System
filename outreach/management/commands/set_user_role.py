from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from outreach.models import Profile


class Command(BaseCommand):
    help = "Set one or more roles for an existing Google-authenticated user."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("role", nargs="+", choices=[choice[0] for choice in Profile.Role.choices])

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        user = get_user_model().objects.filter(email__iexact=email).first()
        if not user:
            raise CommandError(f"No user exists with email {email}. Ask them to sign in once first.")
        profile, _ = Profile.objects.get_or_create(user=user)
        if Profile.Role.ACCOUNT_EXECUTIVE not in options["role"] and (
            user.assigned_escalations.exists() or user.founder_cae_assignments.exists()
        ):
            raise CommandError("Reassign this executive's CRM records and legacy escalation routes before removing their executive role.")
        profile.set_roles(options["role"])
        self.stdout.write(self.style.SUCCESS(f"Set {email} to {profile.roles_display}."))
