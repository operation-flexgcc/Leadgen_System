"""Transactional SendGrid HTTPS delivery; never log credentials or reset URLs."""
import json
from email.utils import parseaddr
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail.backends.base import BaseEmailBackend
from django.core.validators import validate_email
from django.views.decorators.debug import sensitive_variables


class EmailDeliveryError(Exception):
    """Safe operator-facing error, without provider response bodies or secrets."""


def require_email_configuration():
    if settings.EMAIL_BACKEND == "outreach.email_backends.SendGridEmailBackend":
        if not settings.SENDGRID_API_KEY or not settings.SENDGRID_FROM_EMAIL:
            raise EmailDeliveryError("SendGrid API key and verified sender must be configured on the server.")


def email_address(value):
    name, address = parseaddr(value or "")
    try:
        validate_email(address)
    except ValidationError:
        raise EmailDeliveryError("Email address configuration is invalid.") from None
    return {"email": address, **({"name": name} if name else {})}


class SendGridEmailBackend(BaseEmailBackend):
    @sensitive_variables()
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages or []:
            try:
                require_email_configuration()
                if not message.to or message.cc or message.bcc or message.attachments:
                    raise EmailDeliveryError("Account emails require direct recipients and no attachments.")
                # Validate headers using Django, including newline injection checks.
                message.message()
                sender = email_address(message.from_email or settings.DEFAULT_FROM_EMAIL)
                sender.setdefault("name", settings.SENDGRID_FROM_NAME)
                content = [{"type": "text/plain", "value": message.body}]
                for alternative in getattr(message, "alternatives", []):
                    if alternative.mimetype == "text/html":
                        content.append({"type": "text/html", "value": alternative.content})
                payload = {
                    "from": sender,
                    "personalizations": [{"to": [email_address(value) for value in message.to]}],
                    "subject": message.subject,
                    "content": content,
                    "tracking_settings": {
                        "click_tracking": {"enable": False, "enable_text": False},
                        "open_tracking": {"enable": False},
                    },
                }
                request = Request(
                    "https://api.sendgrid.com/v3/mail/send",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}",
                             "Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urlopen(request, timeout=settings.EMAIL_TIMEOUT) as response:
                        if response.status != 202:
                            raise EmailDeliveryError(f"SendGrid did not accept the email (HTTP {response.status}).")
                except HTTPError as error:
                    raise EmailDeliveryError(f"SendGrid rejected the email (HTTP {error.code}).") from None
                except (URLError, TimeoutError, OSError):
                    # Delivery may be ambiguous after a timeout. Do not retry automatically.
                    raise EmailDeliveryError("SendGrid delivery could not be confirmed; check provider activity before retrying.") from None
                sent += 1
            except EmailDeliveryError:
                if not self.fail_silently:
                    raise
        return sent
