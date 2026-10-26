import io
import json
import re
from datetime import timedelta
from unittest.mock import patch, Mock
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from allauth.account.forms import default_token_generator
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model, SESSION_KEY
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from outreach.account_emails import send_password_setup_email
from outreach.adapters import FlexGCCAccountAdapter
from outreach.email_backends import EmailDeliveryError, SendGridEmailBackend
from outreach.models import Profile

OLD_PASSWORD = "Test-only-Old!95gTx3"
NEW_PASSWORD = "Test-only-New!65cDq9"
MEMORY_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SENDGRID_BACKEND = "outreach.email_backends.SendGridEmailBackend"


@override_settings(EMAIL_BACKEND=MEMORY_BACKEND,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AccountEmailTests(TestCase):
    def setUp(self):
        cache.clear()
        self.person = self.make_user("person@example.com", "inside_sales")
        self.admin = self.make_user("admin@example.com", "system_admin")

    def make_user(self, email, role):
        user = get_user_model().objects.create_user(email, email=email, password=OLD_PASSWORD, first_name="Test")
        user.profile.set_roles([role])
        EmailAddress.objects.create(user=user, email=email, primary=True, verified=False)
        return user

    def sent_link(self):
        return re.search(r"https://leadgen\.flexgcc\.com/accounts/password/reset/key/\S+", mail.outbox[-1].body).group()

    def test_individual_mail_has_username_link_and_no_password_and_preserves_account(self):
        original_password = self.person.password
        send_password_setup_email(self.person)
        self.assertEqual(mail.outbox[0].to, [self.person.email])
        self.assertEqual(mail.outbox[0].from_email, "ops@flexgcc.com")
        self.assertIn(self.person.email, mail.outbox[0].body)
        self.assertIn("24 hours", mail.outbox[0].body)
        self.assertIn("https://leadgen.flexgcc.com/login/", mail.outbox[0].body)
        self.assertNotIn(OLD_PASSWORD, mail.outbox[0].body)
        self.sent_link()
        self.person.refresh_from_db()
        self.assertEqual(self.person.password, original_password)
        self.assertEqual(self.person.profile.assigned_roles, ["inside_sales"])

    def test_full_reset_then_login_token_reuse_and_old_session_revocation(self):
        old_session = Client()
        old_session.force_login(self.person)
        send_password_setup_email(self.person)
        path = urlsplit(self.sent_link()).path
        response = self.client.get(path)
        self.assertEqual(response.status_code, 302)
        self.assertIn("set-password", response.url)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("no-store", response["Cache-Control"])
        form_url = response.url
        self.assertContains(self.client.get(form_url), "Set your password")
        response = self.client.post(form_url, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}, follow=True)
        self.assertContains(response, "Password updated")
        self.person.refresh_from_db()
        self.assertTrue(self.person.check_password(NEW_PASSWORD))
        self.assertTrue(EmailAddress.objects.get(user=self.person).verified)
        self.assertContains(Client().get(path), "invalid or expired")
        old_session.get(reverse("dashboard"))
        self.assertNotIn(SESSION_KEY, old_session.session)
        self.client.post(reverse("account_login"), {"login": self.person.email, "password": NEW_PASSWORD})
        self.assertEqual(self.client.session[SESSION_KEY], str(self.person.pk))

    def test_expired_token_cannot_be_used(self):
        past = default_token_generator._now() - timedelta(hours=25)
        with patch.object(type(default_token_generator), "_now", return_value=past):
            send_password_setup_email(self.person)
        self.assertContains(self.client.get(urlsplit(self.sent_link()).path), "invalid or expired")

    def test_disabled_account_cannot_receive_or_use_reset_link(self):
        send_password_setup_email(self.person)
        path = urlsplit(self.sent_link()).path
        self.person.is_active = False
        self.person.save(update_fields=["is_active"])
        self.assertContains(self.client.get(path), "invalid or expired")
        with self.assertRaises(EmailDeliveryError):
            send_password_setup_email(self.person)

    def test_reset_does_not_allow_weak_password_or_missing_csrf(self):
        send_password_setup_email(self.person)
        path = urlsplit(self.sent_link()).path
        form_url = self.client.get(path).url
        self.client.post(form_url, {"password1": "12345678", "password2": "12345678"})
        self.person.refresh_from_db()
        self.assertTrue(self.person.check_password(OLD_PASSWORD))
        secure = Client(enforce_csrf_checks=True)
        form_url = secure.get(path).url
        self.assertEqual(secure.post(form_url, {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}).status_code, 403)

    def test_public_request_has_fixed_origin_and_same_response_for_unknown_or_inactive(self):
        url = reverse("account_reset_password")
        response = self.client.post(url, {"email": self.person.email}, HTTP_HOST="localhost")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.sent_link().startswith("https://leadgen.flexgcc.com/"))
        unknown = self.client.post(url, {"email": "unknown@example.com"})
        self.person.is_active = False
        self.person.save(update_fields=["is_active"])
        inactive = self.client.post(url, {"email": self.person.email})
        self.assertEqual(response.url, unknown.url)
        self.assertEqual(response.url, inactive.url)
        self.assertEqual(len(mail.outbox), 1)

    def test_https_reset_form_preserves_referer_and_accepts_valid_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        url = reverse("account_reset_password")
        response = browser.get(url, secure=True)
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', response.content.decode()).group(1)
        response = browser.post(url, {"email": self.person.email, "csrfmiddlewaretoken": token},
                                secure=True, HTTP_REFERER="https://testserver" + url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=1)
    def test_production_unix_socket_can_request_email_and_log_in(self):
        metadata = {"REMOTE_ADDR": "", "HTTP_X_FORWARDED_FOR": "198.51.100.12"}
        response = self.client.post(reverse("account_reset_password"), {"email": self.person.email}, **metadata)
        self.assertRedirects(response, reverse("account_reset_password_done"))
        self.assertEqual(len(mail.outbox), 1)
        response = self.client.post(reverse("account_login"), {
            "login": self.person.email, "password": OLD_PASSWORD,
        }, **metadata)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session[SESSION_KEY], str(self.person.pk))

    @override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=1, ACCOUNT_RATE_LIMITS={"reset_password": "1/m/ip"})
    def test_spoofing_forwarded_prefix_cannot_bypass_proxy_ip_rate_limit(self):
        url = reverse("account_reset_password")
        first = self.client.post(url, {"email": self.person.email}, REMOTE_ADDR="",
                                 HTTP_X_FORWARDED_FOR="192.0.2.1, 198.51.100.12")
        self.assertEqual(first.status_code, 302)
        second = self.client.post(url, {"email": self.admin.email}, REMOTE_ADDR="",
                                  HTTP_X_FORWARDED_FOR="192.0.2.99, 198.51.100.12")
        self.assertEqual(second.status_code, 429)
        other_visitor = self.client.post(url, {"email": self.admin.email}, REMOTE_ADDR="",
                                        HTTP_X_FORWARDED_FOR="198.51.100.13")
        self.assertEqual(other_visitor.status_code, 302)
        self.assertEqual(len(mail.outbox), 2)

    @override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=1)
    def test_invalid_proxy_ip_fails_closed_without_sending(self):
        response = self.client.post(reverse("account_reset_password"), {"email": self.person.email},
                                    REMOTE_ADDR="", HTTP_X_FORWARDED_FOR="not-an-ip")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(ALLAUTH_TRUSTED_PROXY_COUNT=0)
    def test_direct_development_ignores_forwarded_header(self):
        request = RequestFactory().get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="192.0.2.99")
        self.assertEqual(FlexGCCAccountAdapter().get_client_ip(request), "127.0.0.1")

    @override_settings(EMAIL_BACKEND=SENDGRID_BACKEND, SENDGRID_API_KEY="")
    def test_missing_config_has_safe_error_for_every_public_address(self):
        for email in [self.person.email, "unknown@example.com"]:
            response = self.client.post(reverse("account_reset_password"), {"email": email})
            self.assertContains(response, "temporarily unavailable")
        self.assertEqual(self.client.get(reverse("account_login")).status_code, 200)

    def test_public_provider_failure_does_not_disclose_account_or_secret(self):
        with patch("outreach.account_forms.ResetPasswordForm.save", side_effect=EmailDeliveryError("private detail")):
            with self.assertLogs("outreach.account_forms", level="WARNING") as logs:
                response = self.client.post(reverse("account_reset_password"), {"email": self.person.email})
        self.assertRedirects(response, reverse("account_reset_password_done"))
        self.assertNotIn("private detail", str(logs.output))

    @override_settings(ACCOUNT_RATE_LIMITS={"reset_password": "1/m/key"})
    def test_reset_requests_are_rate_limited(self):
        url = reverse("account_reset_password")
        self.client.post(url, {"email": self.person.email})
        self.assertEqual(self.client.post(url, {"email": self.person.email}).status_code, 429)
        self.assertEqual(len(mail.outbox), 1)

    def test_only_system_admin_can_send_individual_reset_email(self):
        url = reverse("user_send_password_reset", args=[self.person.pk])
        for role in ["intern", "inside_sales", "manager", "account_executive", "ai_user"]:
            actor = self.make_user(f"{role}@example.com", role)
            self.client.force_login(actor)
            self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertRedirects(self.client.post(url), reverse("user_list"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.person.email])

    def test_admin_send_requires_csrf_and_reports_provider_failure(self):
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.admin)
        url = reverse("user_send_password_reset", args=[self.person.pk])
        self.assertEqual(secure.post(url).status_code, 403)
        self.client.force_login(self.admin)
        with patch("outreach.views.send_password_setup_email", side_effect=EmailDeliveryError("SendGrid rejected the email (HTTP 403).")):
            self.assertContains(self.client.post(url, follow=True), "SendGrid rejected")

    def test_command_previews_deduplicates_validates_all_and_sends_separately(self):
        second = self.make_user("second@example.com", "inside_sales")
        output = io.StringIO()
        call_command("send_account_setup_email", self.person.email, stdout=output)
        self.assertIn("Preview only", output.getvalue())
        self.assertEqual(len(mail.outbox), 0)
        with self.assertRaises(CommandError):
            call_command("send_account_setup_email", self.person.email, "missing@example.com", send=True)
        with self.assertRaises(CommandError):
            call_command("send_account_setup_email", self.person.email, role="manager", send=True)
        self.assertEqual(len(mail.outbox), 0)
        call_command("send_account_setup_email", self.person.email, second.email, self.person.email.upper(),
                     role="inside_sales", send=True, stdout=output)
        self.assertEqual([message.to for message in mail.outbox], [[self.person.email], [second.email]])
        self.assertNotIn("/reset/key/", output.getvalue())


@override_settings(EMAIL_BACKEND=SENDGRID_BACKEND, SENDGRID_API_KEY="test-only-sendgrid-key",
                   SENDGRID_FROM_EMAIL="ops@flexgcc.com")
class SendGridBackendTests(TestCase):
    def test_https_payload_and_acceptance_with_tracking_disabled(self):
        message = mail.EmailMultiAlternatives("Setup", "Private link", to=["person@example.com"])
        message.attach_alternative("<p>Private link</p>", "text/html")
        response = Mock(status=202)
        with patch("outreach.email_backends.urlopen") as post:
            post.return_value.__enter__.return_value = response
            self.assertEqual(SendGridEmailBackend().send_messages([message]), 1)
        request = post.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.sendgrid.com/v3/mail/send")
        payload = json.loads(request.data)
        self.assertEqual(payload["from"]["email"], "ops@flexgcc.com")
        self.assertEqual(payload["personalizations"], [{"to": [{"email": "person@example.com"}]}])
        self.assertFalse(payload["tracking_settings"]["click_tracking"]["enable"])
        self.assertFalse(payload["tracking_settings"]["open_tracking"]["enable"])
        self.assertEqual([item["type"] for item in payload["content"]], ["text/plain", "text/html"])

    def test_provider_error_is_sanitized_without_automatic_retry(self):
        error = HTTPError("https://api.sendgrid.com/v3/mail/send", 403, "Forbidden", {}, io.BytesIO(b"secret response"))
        with patch("outreach.email_backends.urlopen", side_effect=error) as post:
            with self.assertRaises(EmailDeliveryError) as result:
                mail.send_mail("Setup", "link", "ops@flexgcc.com", ["person@example.com"])
        self.assertIn("HTTP 403", str(result.exception))
        self.assertNotIn("secret", str(result.exception))
        self.assertEqual(post.call_count, 1)

    def test_timeout_is_ambiguous_and_not_retried(self):
        with patch("outreach.email_backends.urlopen", side_effect=URLError("private network details")) as post:
            with self.assertRaisesMessage(EmailDeliveryError, "could not be confirmed"):
                mail.send_mail("Setup", "link", "ops@flexgcc.com", ["person@example.com"])
        self.assertEqual(post.call_count, 1)

    @override_settings(SENDGRID_API_KEY="")
    def test_missing_key_does_not_call_provider(self):
        with patch("outreach.email_backends.urlopen") as post:
            with self.assertRaises(EmailDeliveryError):
                mail.send_mail("Setup", "link", "ops@flexgcc.com", ["person@example.com"])
            post.assert_not_called()

    def test_nonaccepted_status_and_unsupported_cc_fail(self):
        with patch("outreach.email_backends.urlopen") as post:
            post.return_value.__enter__.return_value = Mock(status=200)
            with self.assertRaises(EmailDeliveryError):
                mail.send_mail("Setup", "link", "ops@flexgcc.com", ["person@example.com"])
        with self.assertRaises(EmailDeliveryError):
            SendGridEmailBackend().send_messages([mail.EmailMessage("Setup", "link", to=["person@example.com"], cc=["other@example.com"])])
