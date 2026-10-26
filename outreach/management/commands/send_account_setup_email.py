from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from outreach.account_emails import send_password_setup_email
from outreach.email_backends import EmailDeliveryError, require_email_configuration
from outreach.models import Profile


class Command(BaseCommand):
    help = "Send individual reset/setup emails to existing accounts (preview unless --send is given)."

    def add_arguments(self, parser):
        parser.add_argument("emails", nargs="+")
        parser.add_argument("--send", action="store_true")
        parser.add_argument("--role", choices=Profile.Role.values)

    def handle(self, *args, **options):
        users = []
        # Validate the entire recipient list before sending anything.
        for email in dict.fromkeys(value.strip().lower() for value in options["emails"]):
            matches = list(get_user_model().objects.filter(email__iexact=email, is_active=True))
            if len(matches) != 1:
                raise CommandError(f"Exactly one active account is required for {email}.")
            user = matches[0]
            if options["role"] and not user.profile.has_role(options["role"]):
                raise CommandError(f"The role for {email} does not match {options['role']}.")
            users.append(user)
        if not options["send"]:
            for user in users:
                self.stdout.write(f"Preview only: {user.get_full_name()} <{user.email}>")
            return
        try:
            require_email_configuration()
            for user in users:
                send_password_setup_email(user)
                self.stdout.write(f"Email provider accepted reset email for {user.email}. Inbox delivery is not yet confirmed.")
        except EmailDeliveryError as error:
            raise CommandError(str(error)) from None
