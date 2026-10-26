"""Real password-login requests and per-person boundaries (test database only)."""
import json
from unittest.mock import Mock

from allauth.account.models import EmailAddress
from allauth.core import context
from allauth.socialaccount.internal.flows.email_authentication import wipe_password
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import SESSION_KEY, get_user_model
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from outreach import crm
from outreach.adapters import FlexGCCSocialAccountAdapter
from outreach.forms import UserProvisionForm
from outreach.middleware import ACTIVE_ROLE_SESSION_KEY
from outreach.models import CompanyUpdateAudit, Profile, Prospect
from outreach.tests.test_company_api import CompanyApiTestMixin


PASSWORD = "Local-QA-only!8Vx2rM47"
NEW_PASSWORD = "Replacement-QA-only!3Kq9sL72"


class PasswordLoginTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.admin = self.make_user("admin@example.com", Profile.Role.SYSTEM_ADMIN)
        self.data = {"full_name": "Separate Person", "email": "person@external.example",
                     "roles": [Profile.Role.INSIDE_SALES], "password1": PASSWORD, "password2": PASSWORD}

    def provision(self, **overrides):
        form = UserProvisionForm({**self.data, **overrides})
        self.assertTrue(form.is_valid(), form.errors)
        return form.save()

    def login(self, user, client=None, password=PASSWORD, **extra):
        return (client or self.client).post(reverse("account_login"), {
            "login": user.email.upper(), "password": password, **extra,
        })

    def test_both_login_routes_render_both_options_and_keep_next(self):
        for name in ["login", "account_login"]:
            response = self.client.get(reverse(name), {"next": "/crm/leads/"})
            self.assertContains(response, 'name="login"')
            self.assertContains(response, 'name="password"')
            self.assertContains(response, 'name="next" value="/crm/leads/"', html=False)
            self.assertContains(response, "Continue with Google")
            self.assertContains(response, "Email (your username)")

    def test_admin_provisions_password_and_external_email_can_login(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_create"), self.data)
        self.assertRedirects(response, reverse("user_list"))
        user = get_user_model().objects.get(email=self.data["email"])
        self.assertTrue(user.check_password(PASSWORD))
        self.assertNotEqual(user.password, PASSWORD)
        self.assertEqual(user.username, user.email)
        self.assertTrue(user.profile.password_provisioned_by_admin)
        self.client.logout()
        response = self.login(user, next="/crm/leads/")
        self.assertRedirects(response, reverse("crm_lead_list"))
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))

    def test_legacy_non_email_username_still_logs_in_by_email(self):
        user = self.provision()
        user.username = "legacy-person"
        user.save(update_fields=["username"])
        self.assertEqual(self.login(user).status_code, 302)
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))

    def test_wrong_missing_unknown_and_disabled_passwords_do_not_login(self):
        user = self.provision()
        for email, password in [(user.email, "wrong"), (user.email, ""), ("unknown@example.com", PASSWORD)]:
            response = self.client.post(reverse("account_login"), {"login": email, "password": password})
            self.assertLess(response.status_code, 500)
            self.assertNotIn(SESSION_KEY, self.client.session)
        user.is_active = False
        user.save(update_fields=["is_active"])
        self.login(user)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_password_login_rejects_csrf_and_unsafe_redirect(self):
        user = self.provision()
        browser = Client(enforce_csrf_checks=True)
        self.assertEqual(self.login(user, client=browser).status_code, 403)
        response = self.login(user, next="https://attacker.example/")
        self.assertEqual(response.url, reverse("dashboard"))

    @override_settings(ACCOUNT_RATE_LIMITS={"login_failed": "2/m/key"})
    def test_failed_password_logins_are_rate_limited(self):
        user = self.provision()
        for _ in range(3):
            response = self.login(user, password="wrong")
        self.assertIn(response.status_code, [200, 429])
        self.assertEqual(self.login(user).status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_public_signup_cannot_create_an_account(self):
        count = get_user_model().objects.count()
        for method in [self.client.get, self.client.post]:
            response = method(reverse("account_signup"), {
                "email": "unapproved@example.com", "password1": PASSWORD, "password2": PASSWORD,
            })
            self.assertContains(response, "Registration is closed")
        self.assertEqual(get_user_model().objects.count(), count)

    @override_settings(SYSTEM_ADMIN_EMAILS={"bootstrap@example.com"}, REQUIRE_PREPROVISIONED_USERS=True)
    def test_closed_local_signup_preserves_approved_google_bootstrap(self):
        adapter = FlexGCCSocialAccountAdapter()
        request = RequestFactory().get("/")
        for email, expected in [("bootstrap@example.com", True), ("stranger@example.com", False)]:
            social = SocialLogin(user=get_user_model()(email=email), account=SocialAccount(provider="google", uid=email))
            self.assertEqual(adapter.is_open_for_signup(request, social), expected)

    def test_google_verified_email_reuses_account_and_preserves_admin_password(self):
        user = self.provision()
        self.assertFalse(EmailAddress.objects.get(user=user).verified)
        provider = Mock(app=None)
        provider.get_settings.return_value = {}
        social = SocialLogin(
            user=get_user_model()(email=user.email), account=SocialAccount(provider="google", uid="test-google-id"),
            email_addresses=[EmailAddress(email=user.email, verified=True, primary=True)], provider=provider,
        )
        request = RequestFactory().get("/")
        with context.request_context(request):
            social.lookup()
            self.assertEqual(social.user.pk, user.pk)
            FlexGCCSocialAccountAdapter().pre_social_login(request, social)
            wipe_password(request, social.user, user.email)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))
        self.assertTrue(EmailAddress.objects.get(user=user).verified)
        self.assertEqual(get_user_model().objects.filter(email=user.email).count(), 1)

    def test_unverified_google_assertion_does_not_verify_local_identity(self):
        user = self.provision()
        social = SocialLogin(user=user, account=SocialAccount(provider="google", uid="unverified"),
                            email_addresses=[EmailAddress(email=user.email, verified=False)])
        FlexGCCSocialAccountAdapter().pre_social_login(RequestFactory().get("/"), social)
        self.assertFalse(EmailAddress.objects.get(user=user).verified)

    def test_google_keeps_password_wipe_protection_for_untrusted_local_password(self):
        user = self.provision()
        Profile.objects.filter(user=user).update(password_provisioned_by_admin=False)
        provider = Mock(app=None)
        provider.get_settings.return_value = {}
        social = SocialLogin(
            user=get_user_model()(email=user.email), account=SocialAccount(provider="google", uid="untrusted-password"),
            email_addresses=[EmailAddress(email=user.email, verified=True, primary=True)], provider=provider,
        )
        request = RequestFactory().get("/")
        with context.request_context(request):
            social.lookup()
            FlexGCCSocialAccountAdapter().pre_social_login(request, social)
            wipe_password(request, social.user, user.email)
        user.refresh_from_db()
        self.assertFalse(user.has_usable_password())

    def test_optional_passwords_validate_and_are_not_echoed(self):
        for password1, password2 in [(PASSWORD, "different"), ("12345678", "12345678"), ("", PASSWORD)]:
            form = UserProvisionForm({**self.data, "password1": password1, "password2": password2})
            self.assertFalse(form.is_valid())
            self.assertNotIn(PASSWORD, form.as_p())
        google_only = self.provision(password1="", password2="")
        self.assertFalse(google_only.has_usable_password())

    def test_blank_password_edit_preserves_password_and_verified_email(self):
        user = self.provision()
        EmailAddress.objects.filter(user=user).update(verified=True)
        form = UserProvisionForm({**self.data, "password1": "", "password2": ""}, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertTrue(user.check_password(PASSWORD))
        self.assertTrue(EmailAddress.objects.get(user=user).verified)

    def test_admin_reset_invalidates_existing_sessions_and_old_password(self):
        user = self.provision()
        other_session = Client()
        self.login(user, client=other_session)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_update", args=[user.pk]), {
            **self.data, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD,
        })
        self.assertRedirects(response, reverse("user_list"))
        response = other_session.get(reverse("crm_lead_list"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(SESSION_KEY, other_session.session)
        self.client.logout()
        self.login(user)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.login(user, password=NEW_PASSWORD)
        self.assertEqual(self.client.session[SESSION_KEY], str(user.pk))

    def test_email_change_revokes_old_password_unless_admin_sets_new_one(self):
        user = self.provision()
        form = UserProvisionForm({**self.data, "email": "new@external.example", "password1": "", "password2": ""}, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.profile.password_provisioned_by_admin)

    def test_password_login_preserves_role_choice_and_isolation(self):
        user = self.provision(roles=["intern", "inside_sales"])
        response = self.client.post(reverse("login"), {"login": user.email, "password": PASSWORD}, follow=True)
        self.assertContains(response, "Choose")
        self.assertNotIn(ACTIVE_ROLE_SESSION_KEY, self.client.session)
        response = self.client.post(reverse("choose_role"), {"role": "manager"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(ACTIVE_ROLE_SESSION_KEY, self.client.session)

    def test_user_can_change_own_password_and_cannot_reset_peer(self):
        user = self.provision()
        self.login(user)
        self.assertContains(self.client.get(reverse("account_change_password")), 'name="oldpassword"')
        response = self.client.post(reverse("account_change_password"), {
            "oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD,
        })
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(self.client.post(reverse("user_update", args=[self.admin.pk]), self.data).status_code, 403)

    def test_google_only_user_can_set_password_and_ai_can_manage_only_own_password(self):
        user = self.provision(password1="", password2="", roles=[Profile.Role.AI_USER])
        self.client.force_login(user)
        self.assertRedirects(self.client.get(reverse("account_change_password")), reverse("account_set_password"))
        response = self.client.post(reverse("account_set_password"), {"password1": PASSWORD, "password2": PASSWORD})
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))
        self.assertEqual(self.client.post(reverse("user_create"), self.data).status_code, 403)


class SeparateSalespersonTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        cache.clear()
        self.cae = self.make_user("cae@example.com", Profile.Role.ACCOUNT_EXECUTIVE)
        self.manager = self.make_user("manager@example.com", Profile.Role.MANAGER)
        self.people = []
        self.leads = []
        for role in [Profile.Role.INTERN, Profile.Role.INSIDE_SALES]:
            for number in [1, 2]:
                user = self.make_user(f"{role}-{number}@external.example", role)
                user.set_password(PASSWORD)
                user.save(update_fields=["password"])
                form, lead = crm.submit_lead(user, {
                    "company_name": f"Private {role} {number}", "contact_name": "Synthetic Contact",
                    "contact_email": "test@example.com", "assigned_to": self.cae.pk,
                    "context": f"Private context {role} {number}",
                })
                self.assertIsNotNone(lead, form.errors)
                self.people.append(user)
                self.leads.append(lead)

    def test_four_separate_password_logins_see_only_their_handoffs_and_exports(self):
        for user, own in zip(self.people, self.leads):
            browser = Client()
            response = browser.post(reverse("account_login"), {"login": user.email, "password": PASSWORD}, follow=True)
            self.assertEqual(browser.session[SESSION_KEY], str(user.pk))
            self.assertContains(response, own.prospect.company_name)
            for url in [reverse("crm_lead_list"), reverse("outreach_history"), reverse("company_export", args=["csv"])]:
                response = browser.get(url)
                self.assertContains(response, own.prospect.company_name)
                for other in self.leads:
                    if other.pk != own.pk:
                        self.assertNotContains(response, other.prospect.company_name)
            for other in self.leads:
                if other.pk == own.pk:
                    continue
                for name, pk in [("crm_lead_detail", other.pk), ("prospect_detail", other.prospect_id), ("prospect_update", other.prospect_id)]:
                    self.assertEqual(browser.get(reverse(name, args=[pk])).status_code, 404)
                self.assertEqual(browser.post(reverse("crm_lead_detail", args=[other.pk]), {}).status_code, 404)
            self.assertEqual(browser.get(reverse("crm_opportunity_list")).status_code, 403)

    def test_frontline_tokens_cannot_list_read_or_update_peer_records(self):
        for user, own in zip(self.people, self.leads):
            auth = {"HTTP_AUTHORIZATION": self.bearer(user)}
            response = self.client.get(reverse("api_crm_leads"), **auth)
            self.assertEqual([item["id"] for item in response.json()["results"]], [own.pk])
            for other in self.leads:
                if other.pk == own.pk:
                    continue
                self.assertEqual(self.client.get(reverse("api_crm_lead_detail", args=[other.pk]), **auth).status_code, 404)
                self.assertEqual(self.client.get(reverse("api_prospect_detail", args=[other.prospect_id]), **auth).status_code, 403)
                response = self.client.patch(reverse("api_prospect_detail", args=[other.prospect_id]),
                                             json.dumps({"comments": "unauthorised"}), content_type="application/json", **auth)
                self.assertEqual(response.status_code, 403)

    def test_unassigned_role_queue_is_shared_until_claimed_but_other_roles_are_hidden(self):
        pool = Prospect.objects.create(company_name="Unassigned test pool", workstream="intern", created_by=self.manager)
        for user in self.people:
            self.client.force_login(user)
            response = self.client.get(reverse("outreach_history"))
            if user.profile.role == "intern":
                self.assertContains(response, pool.company_name)
            else:
                self.assertNotContains(response, pool.company_name)
        self.client.force_login(self.people[0])
        self.assertRedirects(self.client.post(reverse("prospect_claim", args=[pool.pk])), pool.get_absolute_url())
        self.client.force_login(self.people[1])
        self.assertEqual(self.client.get(pool.get_absolute_url()).status_code, 404)

    def test_same_company_api_does_not_expose_or_update_another_owners_channel(self):
        intern = self.people[0]
        inside = self.people[2]
        first = self.make_prospect(intern, "Shared identity", "https://shared-boundary.example.com", contact_email="intern-only@example.com")
        second = self.make_prospect(inside, "Shared identity", "https://shared-boundary.example.com", contact_email="inside-only@example.com")
        self.assertEqual(first.company_id, second.company_id)
        url = reverse("api_company_detail", args=[first.company_id])
        auth = {"HTTP_AUTHORIZATION": self.bearer(intern)}
        response = self.client.get(url, **auth)
        self.assertEqual(response.json()["company"]["prospects"], [{"id": first.pk, "workstream": "intern"}])
        self.assertNotContains(response, second.contact_email)
        response = self.client.patch(url, json.dumps({"email": "intern-updated@example.com"}), content_type="application/json", **auth)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, second.contact_email)
        second.refresh_from_db()
        self.assertEqual(second.contact_email, "inside-only@example.com")
        audit = CompanyUpdateAudit.objects.get(company_id=first.company_id)
        self.assertEqual(audit.previous_values["email"], ["intern-only@example.com"])
