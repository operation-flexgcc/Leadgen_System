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
from outreach.models import ApiAccessToken, Escalation, Opportunity, Outreach, Profile, Prospect, ProspectImportBatch
from outreach.permissions import can_edit_lead, can_view_all_outreach, can_view_crm, is_manager
from outreach.tests.test_company_api import CompanyApiTestMixin


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
        self.assertContains(response, 'name="roles"', count=7)
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
        self.assertContains(response, "+ Add lead")
        self.assertEqual(response.context["current_user_role_label"], "Inside sales")
        self.select("account_executive")
        response = self.client.get(reverse("crm_lead_list"))
        self.assertContains(response, "+ Add lead")
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
        self.assertFalse(can_edit_lead(self.multi, lead))
        _, lead = crm.approve_lead(self.admin, lead.pk, {"version": lead.version})
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


class AIUserRoleTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.ai_user = self.make_user(
            "ai.helper@flexgcc.com", Profile.Role.AI_USER, "AI Helper"
        )
        self.intern = self.make_user("intern@ai-role.test", name="Isha Intern")
        self.inside = self.make_user(
            "inside@ai-role.test", Profile.Role.INSIDE_SALES, "Ivan Inside"
        )
        self.executive = self.make_user(
            "executive@ai-role.test",
            Profile.Role.ACCOUNT_EXECUTIVE,
            "Alex Executive",
        )
        self.admin = self.make_user(
            "admin@ai-role.test", Profile.Role.SYSTEM_ADMIN, "Ada Admin"
        )
        self.first = self.make_prospect(
            self.intern, "Intern Advisory", "https://intern-ai-role.example.com"
        )
        self.second = self.make_prospect(
            self.inside, "Inside Advisory", "https://inside-ai-role.example.com"
        )
        self.lead = Escalation.objects.create(
            prospect=self.first,
            assigned_to=self.executive,
            escalated_by=self.intern,
            source_channel=self.first.workstream,
            context="Qualified handoff for read-only verification.",
        )
        self.opportunity = Opportunity.objects.create(
            escalation=self.lead,
            name="Read-only opportunity",
            requirements="Review the existing requirements.",
        )

    def test_ai_role_has_organization_wide_read_visibility_without_manager_writes(self):
        self.assertTrue(can_view_all_outreach(self.ai_user))
        self.assertTrue(can_view_crm(self.ai_user))
        self.assertFalse(is_manager(self.ai_user))
        self.assertFalse(can_edit_lead(self.ai_user, self.lead))

        self.client.force_login(self.ai_user)
        dashboard = self.client.get(reverse("dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, self.first.company_name)
        self.assertContains(dashboard, self.second.company_name)
        self.assertContains(dashboard, "Team overview")
        self.assertNotContains(dashboard, "+ Add prospect")
        self.assertNotContains(dashboard, "Import CSV")

        users = self.client.get(reverse("user_list"))
        self.assertEqual(users.status_code, 200)
        self.assertContains(users, self.admin.email)
        self.assertContains(users, "View only")
        self.assertNotContains(users, reverse("user_create"))
        self.assertNotContains(users, reverse("user_update", args=[self.admin.pk]))

        lead = self.client.get(self.lead.get_absolute_url())
        self.assertContains(lead, "Read-only view")
        self.assertNotContains(lead, "Save changes")
        self.assertEqual(
            self.client.get(reverse("crm_opportunity_list")).status_code, 200
        )
        opportunity = self.client.get(self.opportunity.get_absolute_url())
        self.assertContains(opportunity, "Read-only view")

    def test_ai_browser_role_cannot_open_or_submit_mutation_workflows(self):
        self.client.force_login(self.ai_user)
        for route, args in [
            ("prospect_create", ()),
            ("prospect_bulk_import", ()),
            ("prospect_update", (self.first.pk,)),
            ("user_create", ()),
            ("user_update", (self.admin.pk,)),
            ("user_delete", (self.admin.pk,)),
            ("crm_submit", ()),
            ("crm_convert", (self.lead.pk,)),
        ]:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route, args=args)).status_code, 403)

        original_name = self.first.company_name
        response = self.client.post(reverse("prospect_claim", args=[self.first.pk]))
        self.assertEqual(response.status_code, 403)
        self.first.refresh_from_db()
        self.assertEqual(self.first.company_name, original_name)

    def test_ai_api_token_can_read_every_scope_and_rejects_all_writes(self):
        token = issue_access_token(self.ai_user)
        client = Client()
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"}

        for route, args in [
            ("api_company_detail", (self.first.company_id,)),
            ("api_prospect_detail", (self.first.pk,)),
            ("api_prospect_outreach_list", (self.second.pk,)),
            ("api_crm_leads", ()),
            ("api_crm_opportunities", ()),
        ]:
            with self.subTest(route=route):
                self.assertEqual(
                    client.get(reverse(route, args=args), **headers).status_code, 200
                )

        attempts = [
            client.patch(
                reverse("api_company_detail", args=[self.first.company_id]),
                json.dumps({"contact_name": "Changed"}),
                content_type="application/json",
                **headers,
            ),
            client.patch(
                reverse("api_prospect_detail", args=[self.first.pk]),
                json.dumps({"comments": "Changed"}),
                content_type="application/json",
                **headers,
            ),
            client.post(
                reverse("api_prospect_outreach_list", args=[self.first.pk]),
                json.dumps(
                    {
                        "activity_type": "follow_up",
                        "medium": "email",
                        "outreach_date": "2026-10-20",
                    }
                ),
                content_type="application/json",
                **headers,
            ),
            client.post(
                reverse("api_crm_leads"),
                json.dumps({"company_name": "Forbidden"}),
                content_type="application/json",
                **headers,
            ),
        ]
        for response in attempts:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"], "AI user access is read-only.")
        self.first.refresh_from_db()
        self.assertEqual(self.first.contact_name, "Asha Rao")
        self.assertEqual(self.first.outreaches.count(), 0)

    def test_admin_can_provision_ai_only_google_login_account(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_create"), {
            "full_name": "AI Read Only",
            "email": "new-ai@ai-role.test",
            "roles": [Profile.Role.AI_USER],
        })
        self.assertRedirects(response, reverse("user_list"))
        user = get_user_model().objects.get(email="new-ai@ai-role.test")
        self.assertEqual(user.profile.assigned_roles, [Profile.Role.AI_USER])
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(executives().filter(pk=user.pk).exists())

    def test_read_only_historical_and_import_views_include_unassigned_records(self):
        batch = ProspectImportBatch.objects.create(
            uploaded_by=self.admin,
            source_filename="read-only-test.csv",
            target_workstream=Profile.Role.INSIDE_SALES,
        )
        self.second.owner = None
        self.second.import_batch = batch
        self.second.save()
        self.client.force_login(self.ai_user)
        for route, args in [
            ("prospect_detail", (self.second.pk,)),
            ("outreach_history", ()),
            ("crm_migration_review", ()),
            ("crm_legacy_routing", ()),
            ("prospect_bulk_import_result", (batch.pk,)),
            ("company_export", ("csv",)),
        ]:
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=args))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Roll back this batch")
                self.assertNotContains(response, "Import another CSV")
        response = self.client.get(reverse("crm_migration_review"))
        self.assertContains(response, "Unassigned")
        self.assertNotContains(response, "Review handoff")
        result = self.client.get(reverse("prospect_bulk_import_result", args=[batch.pk]))
        self.assertFalse(result.context["can_rollback"])

    def test_all_browser_mutation_endpoints_are_denied(self):
        outreach = Outreach.objects.create(
            prospect=self.first, recorded_by=self.intern, sequence_number=1,
            medium="email", outreach_date="2026-10-20",
        )
        batch = ProspectImportBatch.objects.create(
            uploaded_by=self.admin, source_filename="test.csv",
            target_workstream=Profile.Role.INTERN,
        )
        routes = [
            ("prospect_create", ()), ("prospect_update", (self.first.pk,)),
            ("prospect_claim", (self.first.pk,)),
            ("outreach_add", (self.first.pk,)), ("outreach_update", (outreach.pk,)),
            ("prospect_bulk_import", ()), ("prospect_bulk_import_rollback", (batch.pk,)),
            ("user_create", ()), ("user_update", (self.admin.pk,)),
            ("user_delete", (self.admin.pk,)), ("user_restore", (self.admin.pk,)),
            ("crm_submit", ()), ("crm_handoff", (self.second.pk,)),
            ("crm_lead_detail", (self.lead.pk,)), ("crm_convert", (self.lead.pk,)),
            ("crm_comment", (self.lead.pk,)),
            ("crm_opportunity_detail", (self.opportunity.pk,)),
            ("crm_legacy_routing", ()), ("crm_migrate_candidate", (self.second.pk,)),
            ("account_email", ()), ("socialaccount_connections", ()),
        ]
        self.client.force_login(self.ai_user)
        for route, args in routes:
            for method in ["POST", "PUT", "PATCH", "DELETE"]:
                with self.subTest(route=route, method=method):
                    response = self.client.generic(method, reverse(route, args=args), "{}",
                                                   content_type="application/json")
                    self.assertEqual(response.status_code, 403)
                    self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(get_user_model().objects.count(), 5)
        self.assertEqual(Prospect.objects.count(), 2)
        self.assertEqual(Escalation.objects.count(), 1)
        self.assertEqual(Opportunity.objects.count(), 1)
        self.assertEqual(Outreach.objects.count(), 1)
        self.assertEqual(self.lead.activities.count(), 0)
        self.client.logout()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("user_create")).status_code, 200)

    def test_ai_active_role_cannot_edit_previously_owned_records(self):
        self.ai_user.profile.set_roles([Profile.Role.AI_USER, Profile.Role.INTERN])
        Prospect.objects.filter(pk=self.second.pk).update(owner=self.ai_user)
        self.client.force_login(self.ai_user)
        self.client.post(reverse("choose_role"), {"role": Profile.Role.AI_USER})
        response = self.client.get(self.second.get_absolute_url())
        self.assertFalse(response.context["can_edit_prospect"])
        self.assertFalse(response.context["can_add_outreach"])
        self.assertNotContains(response, "Edit prospect")
        self.assertEqual(
            self.client.get(reverse("crm_handoff", args=[self.second.pk])).status_code, 403
        )

    def test_ai_tokens_remain_read_only_in_an_admin_session(self):
        token = issue_access_token(self.ai_user)
        self.client.force_login(self.admin)
        routes = [
            ("api_company_detail", (self.first.company_id,)),
            ("api_company_claim", (self.first.company_id,)),
            ("api_prospect_detail", (self.first.pk,)),
            ("api_prospect_sent", (self.first.pk,)),
            ("api_founder_linkedin", (self.first.pk,)),
            ("api_interest_handoff", (self.first.pk,)),
            ("api_follow_up", (self.first.pk,)),
            ("api_prospect_outreach_list", (self.first.pk,)),
            ("api_outreach_detail", (1,)),
            ("api_crm_leads", ()), ("api_crm_handoff", (self.second.pk,)),
            ("api_crm_lead_detail", (self.lead.pk,)),
            ("api_crm_convert", (self.lead.pk,)),
            ("api_crm_comment", (self.lead.pk,)),
            ("api_crm_opportunities", ()),
            ("api_crm_opportunity_detail", (self.opportunity.pk,)),
        ]
        for route, args in routes:
            for method in ["POST", "PUT", "PATCH", "DELETE"]:
                with self.subTest(route=route, method=method):
                    response = self.client.generic(
                        method, reverse(route, args=args), "{}",
                        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}",
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertEqual(response.json()["error"], "AI user access is read-only.")

    def test_ai_session_keeps_only_self_service_credentials_and_signout(self):
        self.client.force_login(self.ai_user)
        response = self.client.post(reverse("api_access"))
        self.assertEqual(response.status_code, 200)
        token = response.context["token_pair"]["access_token"]
        self.assertEqual(authenticate_access_token(token)._active_role, Profile.Role.AI_USER)
        stored = self.ai_user.api_access_tokens.get()
        response = self.client.post(reverse("api_access_token_revoke", args=[stored.pk]))
        self.assertEqual(response.status_code, 302)
        with self.assertRaises(ApiTokenError):
            authenticate_access_token(token)
        issue_access_token(self.admin)
        other = self.admin.api_access_tokens.get()
        self.assertEqual(
            self.client.post(reverse("api_access_token_revoke", args=[other.pk])).status_code, 404
        )
        self.assertIsNone(ApiAccessToken.objects.get(pk=other.pk).revoked_at)
        self.assertEqual(self.client.post(reverse("account_logout")).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_switching_to_ai_role_never_inherits_other_role_writes(self):
        self.ai_user.profile.set_roles([Profile.Role.AI_USER, Profile.Role.SYSTEM_ADMIN])
        self.client.force_login(self.ai_user)
        self.client.post(reverse("choose_role"), {"role": Profile.Role.AI_USER})
        self.assertEqual(self.client.post(reverse("user_create")).status_code, 403)
        self.client.post(reverse("choose_role"), {"role": Profile.Role.SYSTEM_ADMIN})
        self.assertEqual(self.client.get(reverse("user_create")).status_code, 200)
        self.client.post(reverse("choose_role"), {"role": Profile.Role.AI_USER})
        self.assertEqual(self.client.get(reverse("user_create")).status_code, 403)


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
