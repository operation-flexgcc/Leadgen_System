from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from outreach.models import Profile


class Command(BaseCommand):
    help = "Set an existing Google-authenticated user's FlexGCC Outreach role."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument("role", choices=[choice[0] for choice in Profile.Role.choices])

    def handle(self, *args, **options):
        email = options["email"].strip().lower()
        user = get_user_model().objects.filter(email__iexact=email).first()
        if not user:
            raise CommandError(f"No user exists with email {email}. Ask them to sign in once first.")
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.role = options["role"]
        profile.save(update_fields=["role"])
        self.stdout.write(self.style.SUCCESS(f"Set {email} to {profile.get_role_display()}."))
