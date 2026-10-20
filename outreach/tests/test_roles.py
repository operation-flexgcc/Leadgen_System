import hashlib
import json

from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from outreach import crm
from outreach.adapters import FlexGCCSocialAccountAdapter
from outreach.api_tokens import ApiTokenError, authenticate_access_token, issue_access_token
from outreach.crm_forms import executives
from outreach.forms import ProspectForm, UserProvisionForm
from outreach.middleware import ACTIVE_ROLE_SESSION_KEY
from outreach.models import ApiAccessToken, Escalation, Profile, Prospect
from outreach.permissions import can_edit_lead, is_manager


class MultipleRoleTests(TestCase):
    def make_user(self, name, roles):
        user = get_user_model().objects.create_user(
            username=name, email=f"{name}@example.com", first_name=name.title(),
        )
        user.profile.set_roles(roles)
        return user

    def setUp(self):
        self.admin = self.make_user("admin", ["system_admin"])
        self.multi = self.make_user("multi", ["inside_sales", "account_executive"])
        self.executive = self.make_user("executive", ["account_executive"])

    def select(self, role, client=None, **data):
        return (client or self.client).post(reverse("choose_role"), {"role": role, **data})

    def lead_data(self, name="New firm"):
        return {"company_name": name, "contact_name": "Partner", "contact_email": "partner@example.com",
                "context": "Ready for discovery", "assigned_to": self.multi.pk}

    def submit(self, user=None, name="New firm"):
        user = user or self.multi
        user._active_role = "inside_sales"
        form, lead = crm.submit_lead(user, self.lead_data(name))
        self.assertIsNotNone(lead, form.errors)
        return lead

    def test_admin_can_create_and_edit_multiple_roles(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_create"), {
            "full_name": "Multiple Roles", "email": "new@example.com", "roles": ["inside_sales", "account_executive"],
        })
        self.assertRedirects(response, reverse("user_list"))
        user = get_user_model().objects.get(email="new@example.com")
        self.assertEqual(set(user.profile.assigned_roles), {"inside_sales", "account_executive"})
        response = self.client.get(reverse("user_update", args=[user.pk]))
        self.assertContains(response, 'name="roles"', count=6)
        self.assertEqual(set(response.context["form"].initial["roles"]), {"inside_sales", "account_executive"})
        response = self.client.post(reverse("user_update", args=[user.pk]), {
            "full_name": "Multiple Roles", "email": user.email, "roles": ["intern", "manager"],
        })
        self.assertRedirects(response, reverse("user_list"))
        user.profile.refresh_from_db()
        self.assertEqual(set(user.profile.assigned_roles), {"intern", "manager"})

    def test_role_selection_is_required_for_deep_links_and_posts(self):
        self.client.force_login(self.multi)
        for path in [reverse("dashboard"), reverse("crm_lead_list"), reverse("crm_opportunity_list"), reverse("api_access")]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.url.startswith(reverse("choose_role") + "?next="))
        response = self.client.post(reverse("crm_submit"), self.lead_data())
        self.assertRedirects(response, reverse("choose_role"))
        self.assertFalse(Escalation.objects.exists())

    def test_picker_only_shows_assigned_roles_and_does_not_grant_access(self):
        self.client.force_login(self.multi)
        response = self.client.get(reverse("choose_role"))
        self.assertContains(response, "Inside sales")
        self.assertContains(response, "Client account executive")
        self.assertNotContains(response, 'value="system_admin"')
        self.assertNotContains(response, 'href="/system/users/"')
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])
        for role in ["system_admin", "manager", "", "unknown"]:
            self.assertEqual(self.select(role).status_code, 400)
            self.assertNotIn(ACTIVE_ROLE_SESSION_KEY, self.client.session)

    def test_role_selection_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.multi)
        self.assertEqual(self.select("inside_sales", client=client).status_code, 403)

    def test_single_role_user_skips_picker(self):
        self.client.force_login(self.executive)
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("crm_lead_list"))
        response = self.client.get(reverse("crm_lead_list"))
        self.assertNotContains(response, "Switch role")

    def test_role_selection_landing_and_switch_role_link(self):
        self.client.force_login(self.multi)
        self.assertRedirects(self.select("inside_sales"), reverse("dashboard"), fetch_redirect_response=False)
        response = self.client.get(reverse("crm_lead_list"))
        self.assertContains(response, "Switch role")
        self.assertContains(response, "+ Submit lead")
        self.assertEqual(response.context["current_user_role_label"], "Inside sales")
        self.select("account_executive")
        response = self.client.get(reverse("crm_lead_list"))
        self.assertNotContains(response, "+ Submit lead")
        self.assertEqual(response.context["current_user_role_label"], "Client account executive")

    def test_active_role_not_union_controls_permissions(self):
        self.multi.profile.set_roles(["inside_sales", "system_admin"])
        self.client.force_login(self.multi)
        self.select("inside_sales")
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("crm_opportunity_list")).status_code, 403)
        self.select("system_admin")
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 200)
        self.assertEqual(self.client.get(reverse("crm_opportunity_list")).status_code, 200)
        self.select("inside_sales")
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)

    def test_sessions_select_independently_and_new_login_prompts_again(self):
        second = Client()
        self.client.force_login(self.multi)
        second.force_login(self.multi)
        self.select("inside_sales")
        self.select("account_executive", client=second)
        self.assertEqual(self.client.session[ACTIVE_ROLE_SESSION_KEY], "inside_sales")
        self.assertEqual(second.session[ACTIVE_ROLE_SESSION_KEY], "account_executive")
        self.client.force_login(self.multi)
        self.assertNotIn(ACTIVE_ROLE_SESSION_KEY, self.client.session)
        self.assertTrue(self.client.get(reverse("dashboard")).url.startswith(reverse("choose_role")))

    def test_safe_next_is_preserved_but_external_redirect_is_rejected(self):
        self.client.force_login(self.multi)
        self.assertRedirects(self.select("account_executive", next="/crm/leads/?status=new"), "/crm/leads/?status=new", fetch_redirect_response=False)
        for destination in ["https://evil.example/", "//evil.example/", "/choose-role/", "/login/"]:
            self.assertRedirects(self.select("account_executive", next=destination), reverse("dashboard"), fetch_redirect_response=False)

    def test_removed_selected_role_is_rejected_on_next_request(self):
        self.multi.profile.set_roles(["inside_sales", "account_executive", "manager"])
        self.client.force_login(self.multi)
        self.select("manager")
        self.multi.profile.set_roles(["inside_sales", "account_executive"])
        response = self.client.get(reverse("dashboard"))
        self.assertTrue(response.url.startswith(reverse("choose_role")))
        self.assertNotIn(ACTIVE_ROLE_SESSION_KEY, self.client.session)

    def test_admin_cannot_remove_own_admin_grant_when_it_is_additional(self):
        self.multi.profile.set_roles(["inside_sales", "system_admin"])
        self.client.force_login(self.multi)
        self.select("system_admin")
        response = self.client.post(reverse("user_update", args=[self.multi.pk]), {
            "full_name": "Multi", "email": self.multi.email, "roles": ["inside_sales"],
        })
        self.assertContains(response, "You cannot remove your own")
        self.assertTrue(self.multi.profile.has_role("system_admin"))

    def test_user_summary_counts_each_grant_without_duplicate_people(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("user_list"))
        self.assertEqual(response.context["counts"]["active"], 3)
        self.assertEqual(response.context["counts"]["inside_sales"], 1)
        self.assertEqual(response.context["counts"]["executives"], 2)
        self.assertEqual(response.context["profiles"].count(), 3)

    def test_empty_or_unknown_role_assignment_is_invalid(self):
        for roles in [[], ["superuser"]]:
            form = UserProvisionForm({"full_name": "Invalid", "email": "invalid@example.com", "roles": roles})
            self.assertFalse(form.is_valid())
            self.assertIn("roles", form.errors)

    def test_executive_assignment_uses_any_granted_role_not_current_session(self):
        self.multi._active_role = "inside_sales"
        self.assertIn(self.multi, executives())
        lead = self.submit()
        lead.full_clean()
        self.assertFalse(can_edit_lead(self.multi, lead))
        self.multi._active_role = "account_executive"
        self.assertTrue(can_edit_lead(self.multi, lead))
        form = UserProvisionForm({"full_name": "Multi", "email": self.multi.email, "roles": ["inside_sales"]}, instance=self.multi)
        self.assertFalse(form.is_valid())
        self.assertIn("Reassign", form.errors["roles"][0])

    def test_submission_uses_selected_role_and_keeps_personal_attribution(self):
        self.multi.profile.set_roles(["inside_sales", "intern", "account_executive"])
        self.client.force_login(self.multi)
        for role in ["inside_sales", "intern"]:
            self.select(role)
            data = {**self.lead_data(role + " firm"), "workstream": "linkedin_outreach"}
            response = self.client.post(reverse("crm_submit"), data)
            self.assertEqual(response.status_code, 302)
            lead = Escalation.objects.get(prospect__company_name=role + " firm")
            self.assertEqual(lead.source_channel, role)
            self.assertEqual(lead.escalated_by, self.multi)
            self.assertEqual(lead.prospect.owner, self.multi)
        self.select("inside_sales")
        response = self.client.get(reverse("crm_lead_list"))
        self.assertContains(response, "inside_sales firm")
        self.assertNotContains(response, "intern firm")

    def test_outreach_owner_and_form_accept_additional_role(self):
        self.multi.profile.set_roles(["account_executive", "linkedin_outreach"])
        prospect = Prospect(owner=self.multi, created_by=self.multi, company_name="Owner firm",
                            website="https://owner.example.com", workstream="linkedin_outreach")
        prospect.clean()
        form = ProspectForm(user=self.admin)
        self.assertIn(self.multi, form.fields["owner"].queryset)
        self.assertIn('data-roles="account_executive linkedin_outreach"', str(form["owner"]))

    def test_tokens_are_bound_to_selected_role_and_do_not_gain_added_privileges(self):
        self.multi.profile.set_roles(["inside_sales", "manager"])
        self.client.force_login(self.multi)
        self.select("inside_sales")
        response = self.client.post(reverse("api_access"))
        token = response.context["token_pair"]["access_token"]
        self.assertEqual(ApiAccessToken.objects.get(user=self.multi).role, "inside_sales")
        self.select("manager")
        user = authenticate_access_token(token)
        self.assertFalse(is_manager(user))
        response = self.client.get(reverse("api_crm_opportunities"), HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 403)

    def test_revoked_role_tokens_stay_revoked_if_role_is_readded(self):
        self.multi._active_role = "account_executive"
        token = issue_access_token(self.multi)
        self.multi.profile.set_roles(["inside_sales"])
        self.multi.profile.set_roles(["inside_sales", "account_executive"])
        with self.assertRaises(ApiTokenError):
            authenticate_access_token(token)

    def test_bearer_api_does_not_require_browser_role_selection(self):
        self.multi._active_role = "inside_sales"
        token = issue_access_token(self.multi)
        self.client.force_login(self.multi)
        response = self.client.get(reverse("api_crm_leads"), HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(ACTIVE_ROLE_SESSION_KEY, self.client.session)

    def test_other_role_cannot_modify_owned_outreach_or_handoff_via_api(self):
        lead = self.submit()
        self.multi._active_role = "account_executive"
        token = issue_access_token(self.multi)
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        for name, args in [("api_prospect_detail", [lead.prospect_id]), ("api_company_detail", [lead.prospect.company_id]),
                           ("api_prospect_outreach_list", [lead.prospect_id])]:
            response = self.client.get(reverse(name, args=args), **headers)
            self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse("api_crm_handoff", args=[lead.prospect_id]),
                                    json.dumps({"assigned_to": self.executive.pk, "context": "Attempt"}),
                                    content_type="application/json", **headers)
        self.assertEqual(response.status_code, 403)

    @override_settings(SYSTEM_ADMIN_EMAILS={"multi@example.com"}, MANAGER_EMAILS=set())
    def test_google_bootstrap_retains_other_assigned_roles(self):
        sociallogin = SocialLogin(user=get_user_model()(email=self.multi.email),
                                  account=SocialAccount(provider="google", extra_data={"email": self.multi.email}))
        FlexGCCSocialAccountAdapter().pre_social_login(RequestFactory().get("/"), sociallogin)
        self.multi.profile.refresh_from_db()
        self.assertEqual(set(self.multi.profile.assigned_roles), {"inside_sales", "account_executive", "system_admin"})


class RoleMigrationTests(TransactionTestCase):
    def test_existing_roles_and_token_permissions_are_preserved(self):
        old = ("outreach", "0010_alter_profile_role_alter_prospect_website_escalation_and_more")
        latest = ("outreach", "0011_multiple_user_roles")
        executor = MigrationExecutor(connection)
        executor.migrate([old])
        try:
            apps = executor.loader.project_state([old]).apps
            user = apps.get_model("auth", "User").objects.create(username="migration", email="migration@example.com")
            apps.get_model("outreach", "Profile").objects.create(user_id=user.pk, role="inside_sales")
            token = apps.get_model("outreach", "ApiAccessToken").objects.create(
                user_id=user.pk, token_hash=hashlib.sha256(b"old-token").hexdigest(), token_prefix="old-token",
            )
            executor = MigrationExecutor(connection)
            executor.migrate([latest])
            self.assertEqual(ApiAccessToken.objects.get(pk=token.pk).role, "inside_sales")
            self.assertEqual(Profile.objects.get(user_id=user.pk).assigned_roles, ["inside_sales"])
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
