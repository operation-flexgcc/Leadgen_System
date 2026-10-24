"""Regression coverage for the two 23 October sales feedback emails."""
import json
from datetime import date

from django.core.exceptions import PermissionDenied
from django.forms.models import model_to_dict
from django.test import Client, TestCase
from django.urls import reverse

from outreach import crm, legacy_handoffs
from outreach.crm_forms import LeadUpdateForm, SubmissionForm
from outreach.models import CompanyIdentity, CRMActivity, Escalation, Opportunity, Profile, Prospect
from outreach.tests.test_company_api import CompanyApiTestMixin


class SalesFeedbackTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("admin@feedback.test", Profile.Role.SYSTEM_ADMIN)
        self.manager = self.make_user("manager@feedback.test", Profile.Role.MANAGER)
        self.cae = self.make_user("cae@feedback.test", Profile.Role.ACCOUNT_EXECUTIVE)
        self.sales = self.make_user("sales@feedback.test", Profile.Role.INSIDE_SALES)
        self.linkedin = self.make_user("linkedin@feedback.test", Profile.Role.LINKEDIN_OUTREACH)
        self.intern = self.make_user("intern@feedback.test", Profile.Role.INTERN)
        self.ai = self.make_user("ai@feedback.test", Profile.Role.AI_USER)
        self.data = {"company_name": "Synthetic Feedback Sample", "contact_name": "Test Contact",
                     "contact_email": "sample@example.com", "assigned_to": self.cae.pk,
                     "lead_source": "personal_contact", "first_contact_date": "2026-09-01",
                     "context": "Synthetic testing only."}

    def submit(self, user=None, **overrides):
        form, lead = crm.submit_lead(user or self.sales, {**self.data, **overrides})
        self.assertIsNotNone(lead, form.errors)
        return lead

    def test_all_sales_roles_use_identical_form_and_can_submit(self):
        expected = set(SubmissionForm(user=self.manager).fields)
        for user in [self.intern, self.sales, self.linkedin, self.cae, self.manager, self.admin]:
            with self.subTest(role=user.profile.role):
                self.assertEqual(set(SubmissionForm(user=user).fields), expected)
                self.client.force_login(user)
                self.assertRedirects(self.client.get(reverse("prospect_create")), reverse("crm_submit"))
                self.assertContains(self.client.get(reverse("crm_submit")), "Personal contact")
                self.assertContains(self.client.get(reverse("crm_lead_list")), "+ Add lead")
                lead = self.submit(user, company_name=user.email)
                self.assertEqual(lead.approval_status, "approved" if user in [self.manager, self.admin] else "pending")
                self.assertEqual(lead.prospect.lead_source, "personal_contact")
                self.assertEqual(lead.prospect.first_contact_date, date(2026, 9, 1))

    def test_null_owner_manager_submission_and_opportunity_render(self):
        lead = self.submit(self.manager)
        self.assertIsNone(lead.prospect.owner_id)
        self.client.force_login(self.manager)
        response = self.client.get(lead.get_absolute_url())
        self.assertContains(response, "Unassigned")
        self.assertContains(response, "Personal contact")
        form, opportunity = crm.convert_lead(self.manager, lead.pk, {
            "assigned_to": self.cae.pk, "name": "Sample opportunity", "requirements": "Test",
            "stage": "qualified", "probability": 10, "decision_note": "Test only",
        })
        self.assertIsNotNone(opportunity, form.errors)
        self.assertEqual(self.client.get(opportunity.get_absolute_url()).status_code, 200)

    def test_creation_date_does_not_constrain_first_contact_date(self):
        lead = self.submit(first_contact_date="2030-01-01")
        self.assertEqual(lead.prospect.first_contact_date, date(2030, 1, 1))
        form = SubmissionForm({**self.data, "first_contact_date": "invalid"}, user=self.sales)
        self.assertFalse(form.is_valid())
        self.assertIn("first_contact_date", form.errors)

    def test_pending_requires_manager_and_is_audited_with_stale_protection(self):
        lead = self.submit()
        for user in [self.sales, self.cae, self.ai]:
            with self.assertRaises(PermissionDenied):
                crm.approve_lead(user, lead.pk, {"version": lead.version})
        for operation, data in [(crm.update_lead, {"version": 1, "status": "following_up"}),
                                (crm.convert_lead, {}), (crm.add_comment, {"comment": "No"}),
                                (crm.mark_dead, {"version": 1})]:
            with self.assertRaises(PermissionDenied):
                operation(self.cae, lead.pk, data)
        with self.assertRaises(PermissionDenied):
            crm.convert_lead(self.manager, lead.pk, {})
        form, saved = crm.approve_lead(self.manager, lead.pk, {"version": 0})
        self.assertIsNone(saved)
        form, saved = crm.approve_lead(self.manager, lead.pk, {"version": 1})
        self.assertEqual(saved.approval_status, "approved")
        self.assertEqual(saved.approved_by, self.manager)
        self.assertIsNotNone(saved.approved_at)
        self.assertEqual(saved.activities.filter(kind="approved").count(), 1)
        crm.approve_lead(self.manager, lead.pk, {"version": saved.version})
        self.assertEqual(saved.activities.filter(kind="approved").count(), 1)
        self.client.force_login(self.cae)
        self.assertContains(self.client.get(lead.get_absolute_url()), "Mark dead")

    def test_pending_ui_counts_and_csrf(self):
        lead = self.submit()
        self.client.force_login(self.manager)
        response = self.client.get(reverse("crm_lead_list"), {"approval_status": "pending"})
        self.assertEqual(response.context["summary"]["pending"], 1)
        self.assertEqual(response.context["summary"]["active"], 0)
        self.assertContains(self.client.get(lead.get_absolute_url()), "Approve lead")
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.manager)
        for route in ["crm_approve", "crm_dead"]:
            self.assertEqual(guarded.post(reverse(route, args=[lead.pk]), {"version": 1}).status_code, 403)

    def test_active_role_not_manager_membership_controls_autoapproval(self):
        self.cae.profile.set_roles(["account_executive", "manager"])
        self.cae._active_role = "account_executive"
        self.assertEqual(self.submit(self.cae).approval_status, "pending")
        self.cae._active_role = "manager"
        self.assertEqual(self.submit(self.cae, company_name="Second sample").approval_status, "approved")

    def test_quick_dead_reason_optional_clears_followup_preserves_history(self):
        lead = self.submit(self.manager)
        Escalation.objects.filter(pk=lead.pk).update(next_action="Call", next_action_date="2026-10-25", next_action_owner=self.sales)
        form, saved = crm.mark_dead(self.cae, lead.pk, {"version": lead.version})
        self.assertIsNotNone(saved, form.errors)
        self.assertEqual(saved.status, "dropped")
        self.assertEqual(saved.drop_reason, "")
        self.assertEqual(saved.next_action, "")
        self.assertIsNone(saved.next_action_date)
        self.assertIsNone(saved.next_action_owner)
        self.assertEqual(saved.activities.filter(kind="marked_dead").count(), 1)
        self.assertTrue(Prospect.objects.filter(pk=lead.prospect_id).exists())
        _, stale = crm.mark_dead(self.cae, lead.pk, {"version": lead.version, "reason": "other"})
        self.assertIsNone(stale)
        _, saved = crm.mark_dead(self.cae, lead.pk, {"version": saved.version, "reason": "no_budget"})
        self.assertEqual(saved.drop_reason, "No budget")

    def test_ai_cannot_use_any_new_mutation(self):
        lead = self.submit(self.manager, is_test=True)
        self.client.force_login(self.ai)
        for route in ["crm_submit", "crm_approve", "crm_dead", "crm_remove_test"]:
            args = [] if route == "crm_submit" else [lead.pk]
            self.assertEqual(self.client.post(reverse(route, args=args), {"version": 1, "confirmation": lead.prospect.company_name}).status_code, 403)
        with self.assertRaises(PermissionDenied):
            crm.submit_lead(self.ai, self.data)

    def test_salesperson_and_next_owner_filters_and_sorting(self):
        self.manager.first_name = "Zoe"
        self.manager.save()
        self.sales.first_name = "Amy"
        self.sales.save()
        first = self.submit(self.manager, company_name="Manager sample")
        second = self.submit(self.sales, company_name="Sales sample")
        Escalation.objects.filter(pk=first.pk).update(next_action_owner=self.sales)
        self.client.force_login(self.manager)
        for route in ["crm_lead_list", "dashboard"]:
            response = self.client.get(reverse(route), {"sort": "salesperson"})
            rows = list(response.context["page_obj"])
            self.assertEqual([row.pk for row in rows], [second.pk, first.pk] if route == "crm_lead_list" else [second.prospect_id, first.prospect_id])
            response = self.client.get(reverse(route), {"next_owner": self.sales.pk})
            self.assertEqual(response.context["page_obj"].paginator.count, 1)
        self.assertContains(self.client.get(reverse("crm_lead_list"), {"lead_source": "personal_contact"}), "Manager sample")

    def test_cleanup_only_exact_named_synthetic_records(self):
        real = self.submit(self.manager, company_name="Real customer")
        sample = self.submit(self.manager, is_test=True)
        company_id, prospect_id = sample.prospect.company_id, sample.prospect_id
        for user, lead, name in [(self.manager, sample, sample.prospect.company_name), (self.admin, real, real.prospect.company_name), (self.admin, sample, "wrong")]:
            with self.assertRaises(PermissionDenied):
                crm.remove_test_lead(user, lead.pk, name)
        crm.remove_test_lead(self.admin, sample.pk, sample.prospect.company_name)
        self.assertFalse(Escalation.objects.filter(pk=sample.pk).exists())
        self.assertFalse(Prospect.objects.filter(pk=prospect_id).exists())
        self.assertFalse(CompanyIdentity.objects.filter(pk=company_id).exists())
        self.assertFalse(CRMActivity.objects.filter(escalation_id=sample.pk).exists())
        self.assertTrue(Escalation.objects.filter(pk=real.pk).exists())

    def test_sample_opportunity_cleanup_and_ordinary_record_protection_in_browser(self):
        sample = self.submit(self.manager, is_test=True)
        form, opportunity = crm.convert_lead(self.manager, sample.pk, {
            "assigned_to": self.cae.pk, "name": "Sample", "requirements": "Sample", "stage": "qualified",
            "probability": 0, "decision_note": "Synthetic only",
        })
        self.assertIsNotNone(opportunity, form.errors)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("crm_remove_test", args=[sample.pk])), "exact company name")
        self.assertRedirects(self.client.post(reverse("crm_remove_test", args=[sample.pk]), {"confirmation": sample.prospect.company_name}), reverse("crm_lead_list"))
        self.assertFalse(Opportunity.objects.filter(pk=opportunity.pk).exists())
        real = self.submit(self.manager, company_name="Not a test")
        self.assertEqual(self.client.post(reverse("crm_remove_test", args=[real.pk]), {"confirmation": real.prospect.company_name}).status_code, 404)

    def test_dashboard_current_followup_matches_crm_and_dead_is_not_overdue(self):
        lead = self.submit(self.manager)
        Prospect.objects.filter(pk=lead.prospect_id).update(next_action="Old outreach", next_action_date="2026-01-01")
        Escalation.objects.filter(pk=lead.pk).update(next_action="Current CAE action", next_action_date="2026-02-01", next_action_owner=self.cae)
        self.client.force_login(self.manager)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Current CAE action")
        self.assertNotContains(response, "Old outreach")
        crm.mark_dead(self.manager, lead.pk, {"version": lead.version})
        response = self.client.get(reverse("dashboard"), {"due": "overdue"})
        self.assertEqual(response.context["summary"]["overdue"], 0)
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_pipeline_next_step_owner_sort_uses_opportunity_not_old_lead_owner(self):
        lead = self.submit(self.manager)
        Escalation.objects.filter(pk=lead.pk).update(next_action_owner=self.sales)
        form, opportunity = crm.convert_lead(self.manager, lead.pk, {
            "assigned_to": self.cae.pk, "name": "Sample", "requirements": "Sample", "stage": "qualified",
            "probability": 0, "decision_note": "Synthetic only", "next_action_owner": self.intern.pk,
        })
        self.assertIsNotNone(opportunity, form.errors)
        self.client.force_login(self.manager)
        for route in ["dashboard", "crm_lead_list", "crm_opportunity_list"]:
            response = self.client.get(reverse(route), {"next_owner": self.intern.pk, "sort": "-next_owner"})
            self.assertEqual(response.context["page_obj"].paginator.count, 1)
            response = self.client.get(reverse(route), {"next_owner": self.sales.pk})
            self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_cleanup_does_not_remove_shared_company_identity(self):
        sample = self.submit(self.manager, is_test=True, website="https://shared.example.com")
        real = self.make_prospect(self.linkedin, "Real shared company", "https://shared.example.com")
        self.assertEqual(sample.prospect.company_id, real.company_id)
        crm.remove_test_lead(self.admin, sample.pk, sample.prospect.company_name)
        self.assertTrue(CompanyIdentity.objects.filter(pk=real.company_id).exists())

    def test_api_cannot_skip_approval_or_change_immutable_test_marker(self):
        result = self.client.post(reverse("api_crm_leads"), json.dumps(self.data), content_type="application/json", HTTP_AUTHORIZATION=self.bearer(self.sales))
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()["approval_status"], "pending")
        self.assertEqual(result.json()["prospect"]["lead_source"], "personal_contact")
        pk = result.json()["id"]
        result = self.client.patch(reverse("api_crm_lead_detail", args=[pk]), json.dumps({"version": 1, "status": "following_up"}), content_type="application/json", HTTP_AUTHORIZATION=self.bearer(self.cae))
        self.assertEqual(result.status_code, 403)
        result = self.client.patch(reverse("api_crm_lead_detail", args=[pk]), json.dumps({"version": 1, "approval_status": "approved"}), content_type="application/json", HTTP_AUTHORIZATION=self.bearer(self.manager))
        self.assertEqual(result.status_code, 400)
