import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.forms.models import model_to_dict
from django.db import close_old_connections
from django.test import (
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.urls import reverse
from django.utils import timezone

from outreach import crm
from outreach.adapters import FlexGCCSocialAccountAdapter
from outreach.crm_forms import LeadUpdateForm, OpportunityForm
from outreach.forms import ProspectForm, UserProvisionForm
from outreach.models import CRMActivity, Escalation, Opportunity, Outreach, Profile
from outreach.permissions import is_manager
from outreach.tests.test_company_api import CompanyApiTestMixin
from outreach.workflows import sync_prospect_from_outreach


class CRMTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user("intern@crm.test")
        self.inside = self.make_user("inside@crm.test", Profile.Role.INSIDE_SALES)
        self.linkedin = self.make_user(
            "linkedin@crm.test", Profile.Role.LINKEDIN_OUTREACH
        )
        self.ae = self.make_user("executive@crm.test", Profile.Role.ACCOUNT_EXECUTIVE)
        self.other = self.make_user("other@crm.test", Profile.Role.ACCOUNT_EXECUTIVE)
        self.manager = self.make_user("manager@crm.test", Profile.Role.MANAGER)
        self.admin = self.make_user("admin@crm.test", Profile.Role.SYSTEM_ADMIN)
        self.prospect = self.make_prospect(
            self.linkedin, "Example Advisory", "https://advisory.example.com"
        )

    def handoff(self, **data):
        form, lead = crm.escalate_prospect(
            self.linkedin,
            self.prospect.pk,
            {
                "assigned_to": self.ae.pk,
                "context": "Client replied and requested discovery.",
                "response_confirmed": True,
                **data,
            },
        )
        self.assertIsNotNone(lead, form.errors)
        return lead

    def update_data(self, lead, **overrides):
        return {
            **model_to_dict(lead, fields=LeadUpdateForm.Meta.fields),
            "version": lead.version,
            **overrides,
        }

    def convert(self, lead, **overrides):
        form, record = crm.convert_lead(
            self.ae,
            lead.pk,
            {
                "name": "Advisory opportunity",
                "requirements": "Build a GCC team",
                "stage": "qualified",
                "probability": 25,
                "decision_note": "Discovery confirmed a fit.",
                **overrides,
            },
        )
        self.assertIsNotNone(record, form.errors)
        return record

    def api(self, method, route, user=None, args=(), data=None, query=None):
        options = {"HTTP_AUTHORIZATION": self.bearer(user or self.ae)}
        if method == "get":
            return self.client.get(reverse(route, args=args), query or {}, **options)
        return getattr(self.client, method)(
            reverse(route, args=args),
            json.dumps(data or {}),
            content_type="application/json",
            **options,
        )

    def test_simple_submission_without_research_or_website(self):
        data = {
            "company_name": "New firm",
            "contact_name": "Partner",
            "contact_email": "partner@example.com",
            "context": "Ready for a discovery call.",
            "assigned_to": self.ae.pk,
        }
        form, lead = crm.submit_lead(self.inside, data)
        self.assertIsNotNone(lead, form.errors)
        self.assertEqual(lead.source_channel, "inside_sales")
        self.assertEqual(lead.prospect.owner, self.inside)
        self.assertEqual(lead.prospect.eligibility_evidence, "")
        self.assertTrue(lead.prospect.company_id)
        self.assertEqual(lead.prospect.outreaches.count(), 0)
        form, duplicate = crm.submit_lead(self.inside, data)
        self.assertIsNone(duplicate)
        self.assertEqual(Escalation.objects.count(), 1)

    def test_submission_requires_contact_context_and_active_executive(self):
        self.ae.is_active = False
        self.ae.save()
        form, lead = crm.submit_lead(
            self.intern, {"company_name": "Missing data", "assigned_to": self.ae.pk}
        )
        self.assertIsNone(lead)
        for field in ["contact_name", "contact_email", "context", "assigned_to"]:
            self.assertIn(field, form.errors)

    def test_simple_submission_contact_correction_needs_no_research(self):
        form, lead = crm.submit_lead(
            self.intern,
            {
                "company_name": "Simple firm",
                "contact_name": "Partner",
                "contact_phone": "12345",
                "assigned_to": self.ae.pk,
                "context": "Ready",
            },
        )
        self.assertIsNotNone(lead, form.errors)
        form = ProspectForm(
            {**model_to_dict(lead.prospect), "contact_name": "Corrected name"},
            instance=lead.prospect,
            user=self.intern,
        )
        self.assertTrue(form.is_valid(), form.errors)
        source = form.save()
        self.assertEqual(source.contact_name, "Corrected name")
        self.assertEqual(source.owner_id, self.intern.pk)
        self.assertEqual(source.escalation.assigned_to_id, self.ae.pk)

    def test_linkedin_handoff_explicit_idempotent_and_preserves_owner(self):
        form, rejected = crm.escalate_prospect(
            self.linkedin,
            self.prospect.pk,
            {"context": "Reply received", "assigned_to": self.ae.pk},
        )
        self.assertIsNone(rejected)
        self.assertIn("response_confirmed", form.errors)
        lead = self.handoff()
        same = self.handoff()
        self.assertEqual(lead.pk, same.pk)
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.owner, self.linkedin)
        self.assertEqual(lead.escalated_by, self.linkedin)
        self.assertEqual(lead.activities.count(), 1)

    def test_outreach_notes_do_not_automatically_create_handoff(self):
        outreach = Outreach.objects.create(
            prospect=self.prospect,
            recorded_by=self.linkedin,
            sequence_number=1,
            activity_type="inbound_response",
            medium="linkedin",
            outreach_date=timezone.localdate(),
            response="Client replied.",
        )
        sync_prospect_from_outreach(self.prospect, outreach)
        self.assertFalse(Escalation.objects.exists())

    def test_ae_shared_visibility_but_assigned_only_writes(self):
        lead = self.handoff()
        self.client.force_login(self.other)
        self.assertContains(
            self.client.get(reverse("crm_lead_list")), self.prospect.company_name
        )
        response = self.client.get(lead.get_absolute_url())
        self.assertContains(response, "Read-only view")
        self.assertNotContains(response, "Save changes")
        self.assertEqual(
            self.client.post(
                lead.get_absolute_url(), {"version": 1, "status": "following_up"}
            ).status_code,
            403,
        )
        with self.assertRaises(PermissionDenied):
            crm.add_comment(self.other, lead.pk, {"comment": "Not mine"})

    def test_source_visibility_is_own_only_and_read_only(self):
        lead = self.handoff()
        self.client.force_login(self.linkedin)
        self.assertContains(self.client.get(lead.get_absolute_url()), "Client replied")
        self.client.force_login(self.intern)
        self.assertEqual(self.client.get(lead.get_absolute_url()).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("crm_opportunity_list")).status_code, 403
        )

    def test_admin_reassignment_immediately_revokes_old_editor(self):
        lead = self.handoff()
        form, saved = crm.update_lead(
            self.manager, lead.pk, self.update_data(lead, assigned_to=self.other.pk)
        )
        self.assertIsNotNone(saved, form.errors)
        with self.assertRaises(PermissionDenied):
            crm.update_lead(self.ae, lead.pk, self.update_data(saved))
        self.assertEqual(
            saved.activities.first().changes["assigned_to_id"]["after"],
            str(self.other.pk),
        )

    def test_stale_lead_update_is_rejected_without_audit_or_data_loss(self):
        lead = self.handoff()
        data = self.update_data(lead, status="following_up")
        form, saved = crm.update_lead(self.ae, lead.pk, data)
        self.assertIsNotNone(saved, form.errors)
        form, stale = crm.update_lead(self.ae, lead.pk, {**data, "status": "new"})
        self.assertIsNone(stale)
        self.assertIn("changed while", str(form.errors))
        lead.refresh_from_db()
        self.assertEqual(lead.status, "following_up")
        self.assertEqual(lead.activities.count(), 2)

    def test_followup_validation_and_timezone(self):
        lead = self.handoff()
        cases = [
            ("next_action_date", {"next_action": "Call"}),
            ("drop_reason", {"status": "dropped"}),
            ("discovery_outcome", {"status": "discovery_completed"}),
            ("discovery_at", {"status": "discovery_scheduled"}),
            ("discovery_timezone", {"discovery_timezone": "Invalid/City"}),
        ]
        for field, changes in cases:
            with self.subTest(field=field):
                form, saved = crm.update_lead(
                    self.ae, lead.pk, self.update_data(lead, **changes)
                )
                self.assertIsNone(saved)
                self.assertIn(field, form.errors)
        form, saved = crm.update_lead(
            self.ae,
            lead.pk,
            self.update_data(
                lead,
                status="discovery_scheduled",
                discovery_at="2026-10-20T09:00",
                discovery_timezone="America/Chicago",
            ),
        )
        self.assertIsNotNone(saved, form.errors)
        saved.refresh_from_db()
        self.assertEqual(saved.discovery_at.hour, 14)

    def test_ambiguous_dst_time_rejected(self):
        lead = self.handoff()
        form, saved = crm.update_lead(
            self.ae,
            lead.pk,
            self.update_data(
                lead,
                discovery_at="2026-11-01T01:30",
                discovery_timezone="America/Chicago",
            ),
        )
        self.assertIsNone(saved)
        self.assertIn("discovery_at", form.errors)

    @override_settings(TIME_ZONE="America/Chicago")
    def test_meeting_uses_explicit_zone_not_server_dst(self):
        lead = self.handoff()
        form, saved = crm.update_lead(
            self.ae,
            lead.pk,
            self.update_data(
                lead, discovery_at="2026-03-08T02:30", discovery_timezone="Asia/Kolkata"
            ),
        )
        self.assertIsNotNone(saved, form.errors)

    @override_settings(
        SYSTEM_ADMIN_EMAILS=set(),
        MANAGER_EMAILS=set(),
        REQUIRE_PREPROVISIONED_USERS=True,
    )
    def test_google_login_preserves_provisioned_executive_role(self):
        sociallogin = SocialLogin(
            user=get_user_model()(email=self.ae.email),
            account=SocialAccount(
                provider="google", extra_data={"email": self.ae.email}
            ),
        )
        FlexGCCSocialAccountAdapter().pre_social_login(
            RequestFactory().get("/"), sociallogin
        )
        self.ae.profile.refresh_from_db()
        self.assertEqual(self.ae.profile.role, "account_executive")

    def test_drop_and_reopen_no_automatic_timeout(self):
        lead = self.handoff()
        Escalation.objects.filter(pk=lead.pk).update(
            escalated_at=timezone.now() - timedelta(days=180)
        )
        lead.refresh_from_db()
        self.assertEqual(lead.status, "new")
        form, dropped = crm.update_lead(
            self.ae,
            lead.pk,
            self.update_data(lead, status="dropped", drop_reason="Not ready"),
        )
        self.assertIsNotNone(dropped, form.errors)
        form, rejected = crm.convert_lead(
            self.ae,
            lead.pk,
            {
                "name": "Deal",
                "requirements": "Team",
                "stage": "qualified",
                "probability": 20,
                "decision_note": "Fit",
            },
        )
        self.assertIsNone(rejected)
        self.assertIn("Reopen", str(form.errors))

    def test_conversion_is_linked_idempotent_and_blocks_queue_edit(self):
        lead = self.handoff()
        record = self.convert(lead)
        self.assertEqual(self.convert(lead).pk, record.pk)
        lead.refresh_from_db()
        self.assertEqual(lead.status, "converted")
        self.assertEqual(record.escalation_id, lead.pk)
        self.assertEqual(self.prospect.owner_id, self.linkedin.pk)
        with self.assertRaises(PermissionDenied):
            crm.update_lead(self.ae, lead.pk, self.update_data(lead))

    def test_opportunity_close_reopen_audit_and_version(self):
        lead = self.handoff()
        record = self.convert(lead, estimated_value="12000.50", currency="usd")
        data = {
            **model_to_dict(record, fields=OpportunityForm.Meta.fields),
            "version": record.version,
            "stage": "won",
        }
        form, saved = crm.update_opportunity(self.ae, record.pk, data)
        self.assertIsNone(saved)
        self.assertIn("closure_reason", form.errors)
        form, saved = crm.update_opportunity(
            self.ae, record.pk, {**data, "closure_reason": "Contract signed"}
        )
        self.assertIsNotNone(saved, form.errors)
        self.assertEqual(saved.probability, 100)
        self.assertIsNotNone(saved.closed_at)
        form, stale = crm.update_opportunity(
            self.ae, record.pk, {**data, "closure_reason": "Old edit"}
        )
        self.assertIsNone(stale)
        data.update(
            version=saved.version,
            stage="negotiation",
            probability=80,
            closure_reason="Reopened",
        )
        form, reopened = crm.update_opportunity(self.ae, record.pk, data)
        self.assertIsNotNone(reopened, form.errors)
        self.assertIsNone(reopened.closed_at)

    def test_opportunity_numeric_limits(self):
        lead = self.handoff()
        for changes, field in [
            ({"probability": 101}, "probability"),
            ({"estimated_value": "-1", "currency": "USD"}, "estimated_value"),
            ({"estimated_value": "20", "currency": ""}, "currency"),
        ]:
            form, record = crm.convert_lead(
                self.ae,
                lead.pk,
                {
                    "name": "Deal",
                    "requirements": "Team",
                    "stage": "qualified",
                    "probability": 20,
                    "decision_note": "Fit",
                    **changes,
                },
            )
            self.assertIsNone(record)
            self.assertIn(field, form.errors)

    def test_historical_review_is_admin_only_and_preserves_unknown_date(self):
        before = model_to_dict(self.prospect)
        self.prospect.interest_signal = "Interested"
        self.prospect.save()
        before["interest_signal"] = "Interested"
        data = {"assigned_to": self.ae.pk, "context": "Historical discovery follow-up"}
        with self.assertRaises(PermissionDenied):
            crm.escalate_prospect(self.manager, self.prospect.pk, data, historical=True)
        form, lead = crm.escalate_prospect(
            self.admin, self.prospect.pk, data, historical=True
        )
        self.assertIsNotNone(lead, form.errors)
        self.assertTrue(lead.historical)
        self.assertIsNone(lead.escalated_at)
        self.assertNotIn(self.prospect, crm.review_candidates())
        self.prospect.refresh_from_db()
        self.assertEqual(model_to_dict(self.prospect), before)

    def test_historical_invalid_original_action_reports_error_not_500(self):
        self.prospect.next_action = "Call"
        self.prospect.save()
        form, lead = crm.escalate_prospect(
            self.admin,
            self.prospect.pk,
            {"assigned_to": self.ae.pk, "context": "Reviewed"},
            historical=True,
        )
        self.assertIsNone(lead)
        self.assertIn("Correct the original", str(form.errors))

    def test_role_provisioning_and_role_change_guard(self):
        self.handoff()
        form = UserProvisionForm(
            {
                "full_name": "New Executive",
                "email": "new@crm.test",
                "role": "account_executive",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertFalse(user.has_usable_password())
        self.assertFalse(is_manager(user))
        self.assertEqual(user.profile.role, "account_executive")
        form = UserProvisionForm(
            {"full_name": "Old Executive", "email": self.ae.email, "role": "intern"},
            instance=self.ae,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("role", form.errors)

    def test_queue_filters_exclude_terminal_overdue(self):
        lead = self.handoff()
        Escalation.objects.filter(pk=lead.pk).update(
            next_action="Call",
            next_action_date=timezone.localdate() - timedelta(days=1),
            status="dropped",
            drop_reason="No fit",
        )
        self.client.force_login(self.ae)
        response = self.client.get(reverse("crm_lead_list"), {"due": "overdue"})
        self.assertEqual(response.context["summary"]["total"], 0)
        response = self.client.get(
            reverse("crm_lead_list"),
            {"date_from": "2026-12-01", "date_to": "2026-01-01"},
        )
        self.assertEqual(response.context["summary"]["total"], 0)
        self.assertTrue(response.context["filter_form"].errors)

    def test_new_pages_render_and_login_routes_by_role(self):
        lead = self.handoff()
        record = self.convert(lead)
        self.client.force_login(self.admin)
        for route, args in [
            ("crm_lead_list", []),
            ("crm_submit", []),
            ("crm_lead_detail", [lead.pk]),
            ("crm_opportunity_list", []),
            ("crm_opportunity_detail", [record.pk]),
            ("crm_migration_review", []),
            ("crm_api_guide", []),
        ]:
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(reverse(route, args=args)).status_code, 200
                )
        for user in [self.intern, self.inside, self.ae]:
            self.client.force_login(user)
            self.assertRedirects(
                self.client.get(reverse("dashboard")), reverse("crm_lead_list")
            )
        self.client.force_login(self.linkedin)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_api_partial_updates_preserve_fields_and_require_version(self):
        lead = self.handoff()
        result = self.api(
            "patch",
            "api_crm_lead_detail",
            args=[lead.pk],
            data={"status": "following_up"},
        )
        self.assertEqual(result.status_code, 400)
        result = self.api(
            "patch",
            "api_crm_lead_detail",
            args=[lead.pk],
            data={"status": "following_up", "version": 1},
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["context"], lead.context)
        self.assertEqual(result.json()["version"], 2)
        result = self.api(
            "patch",
            "api_crm_lead_detail",
            args=[lead.pk],
            data={"status": "new", "version": 1},
        )
        self.assertEqual(result.status_code, 409)

    def test_api_enforces_scopes_fields_and_json_types(self):
        lead = self.handoff()
        for changes in [
            {"assigned_to": self.other.pk, "version": 1},
            {"version": True, "status": "new"},
            {"context": "overwrite", "version": 1},
            {"status": ["new"], "version": 1},
        ]:
            result = self.api(
                "patch", "api_crm_lead_detail", args=[lead.pk], data=changes
            )
            self.assertEqual(result.status_code, 400, result.content)
        self.assertEqual(
            self.api("get", "api_crm_lead_detail", self.intern, [lead.pk]).status_code,
            404,
        )
        self.assertEqual(
            self.api(
                "patch",
                "api_crm_lead_detail",
                self.other,
                [lead.pk],
                {"status": "new", "version": 1},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.api(
                "post", "api_crm_handoff", self.intern, [self.prospect.pk], {}
            ).status_code,
            403,
        )
        self.assertEqual(self.client.get(reverse("api_crm_leads")).status_code, 401)

    def test_api_datetime_offset_conversion_and_partial_update(self):
        lead = self.handoff()
        result = self.api(
            "patch",
            "api_crm_lead_detail",
            args=[lead.pk],
            data={
                "version": 1,
                "status": "discovery_scheduled",
                "discovery_at": "2026-10-20T14:00:00Z",
                "discovery_timezone": "America/Chicago",
            },
        )
        self.assertEqual(result.status_code, 200, result.content)
        result = self.api(
            "patch",
            "api_crm_lead_detail",
            args=[lead.pk],
            data={"version": 2, "comment": "Confirmed"},
        )
        self.assertEqual(result.status_code, 200, result.content)
        lead.refresh_from_db()
        self.assertEqual(lead.discovery_at.hour, 14)

    def test_api_submission_conversion_opportunity_and_audit(self):
        result = self.api(
            "post",
            "api_crm_leads",
            self.intern,
            data={
                "company_name": "API firm",
                "contact_name": "Partner",
                "contact_phone": "+1 555 0100",
                "context": "Ready",
                "assigned_to": self.ae.pk,
            },
        )
        self.assertEqual(result.status_code, 201, result.content)
        lead_id = result.json()["id"]
        result = self.api(
            "post",
            "api_crm_convert",
            args=[lead_id],
            data={
                "name": "API deal",
                "requirements": "Team",
                "stage": "qualified",
                "probability": 20,
                "decision_note": "Fit",
            },
        )
        self.assertEqual(result.status_code, 200, result.content)
        opportunity_id = result.json()["id"]
        result = self.api(
            "patch",
            "api_crm_opportunity_detail",
            self.manager,
            [opportunity_id],
            {"version": 1, "assigned_to": self.other.pk, "stage": "proposal"},
        )
        self.assertEqual(result.status_code, 200, result.content)
        result = self.api(
            "post",
            "api_crm_comment",
            self.other,
            [lead_id],
            {"comment": "Proposal sent"},
        )
        self.assertEqual(result.status_code, 201)
        events = self.api("get", "api_crm_activity", self.ae, [lead_id]).json()
        self.assertEqual(events["results"][0]["comment"], "Proposal sent")
        self.assertEqual(
            self.api("get", "api_crm_opportunities", self.intern).status_code, 403
        )
        self.assertEqual(self.api("get", "api_crm_opportunities").json()["count"], 1)

    def test_pipeline_summaries_keep_currencies_separate(self):
        self.convert(self.handoff(), estimated_value="100.00", currency="USD")
        other_prospect = self.make_prospect(
            self.linkedin, "Another firm", "https://another.example.com"
        )
        form, lead = crm.escalate_prospect(
            self.linkedin,
            other_prospect.pk,
            {"assigned_to": self.ae.pk, "context": "Ready", "response_confirmed": True},
        )
        self.assertIsNotNone(lead, form.errors)
        self.convert(lead, estimated_value="200.00", currency="INR")
        self.client.force_login(self.ae)
        response = self.client.get(reverse("crm_opportunity_list"))
        self.assertEqual(
            {
                row["currency"]: row["total"]
                for row in response.context["values_by_currency"]
            },
            {"USD": Decimal("100"), "INR": Decimal("200")},
        )
        response = self.client.get(
            reverse("crm_opportunity_list"), {"source_channel": "inside_sales"}
        )
        self.assertEqual(response.context["summary"]["total"], 0)

    def test_manager_api_conversion_keeps_existing_executive_by_default(self):
        lead = self.handoff()
        result = self.api(
            "post",
            "api_crm_convert",
            self.manager,
            [lead.pk],
            {
                "name": "Managed deal",
                "requirements": "Team",
                "stage": "qualified",
                "probability": 25,
                "decision_note": "Ready",
            },
        )
        self.assertEqual(result.status_code, 200, result.content)
        self.assertEqual(result.json()["lead"]["assigned_to"]["id"], self.ae.pk)

    def test_comments_are_escaped_and_full_history_is_paginated(self):
        lead = self.handoff()
        for index in range(27):
            CRMActivity.objects.create(
                escalation=lead, actor=self.ae, kind="comment", comment=f"Note {index}"
            )
        crm.add_comment(self.ae, lead.pk, {"comment": "<script>alert('x')</script>"})
        self.client.force_login(self.ae)
        response = self.client.get(lead.get_absolute_url())
        self.assertNotContains(response, "<script>alert")
        self.assertContains(response, "&lt;script&gt;")
        self.assertEqual(len(response.context["activities"]), 25)
        self.assertEqual(
            len(
                self.client.get(lead.get_absolute_url(), {"activity_page": 2}).context[
                    "activities"
                ]
            ),
            4,
        )


@skipUnlessDBFeature("has_select_for_update")
class CRMConcurrencyTests(CompanyApiTestMixin, TransactionTestCase):
    def setUp(self):
        self.operator = self.make_user(
            "race-operator@crm.test", Profile.Role.LINKEDIN_OUTREACH
        )
        self.executive = self.make_user(
            "race-executive@crm.test", Profile.Role.ACCOUNT_EXECUTIVE
        )
        self.prospect = self.make_prospect(
            self.operator, "Concurrent handoff", "https://race.example.com"
        )

    def parallel(self, operation):
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            return [future.result(timeout=20) for future in futures]

    def handoff(self):
        _, lead = crm.escalate_prospect(
            self.operator,
            self.prospect.pk,
            {
                "assigned_to": self.executive.pk,
                "context": "Ready",
                "response_confirmed": True,
            },
        )
        return lead.pk

    def test_concurrent_handoffs_create_one_record_and_one_event(self):
        identifiers = self.parallel(self.handoff)
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(Escalation.objects.count(), 1)
        self.assertEqual(CRMActivity.objects.count(), 1)

    def test_concurrent_conversions_create_one_opportunity(self):
        lead_id = self.handoff()

        def convert():
            _, record = crm.convert_lead(
                self.executive,
                lead_id,
                {
                    "name": "Deal",
                    "requirements": "Team",
                    "stage": "qualified",
                    "probability": 20,
                    "decision_note": "Qualified",
                },
            )
            return record.pk

        self.assertEqual(len(set(self.parallel(convert))), 1)
        self.assertEqual(Opportunity.objects.count(), 1)
        self.assertEqual(CRMActivity.objects.filter(kind="converted").count(), 1)

    def test_concurrent_updates_accept_only_one_version(self):
        lead_id = self.handoff()

        def update():
            _, saved = crm.update_lead(
                self.executive, lead_id, {"version": 1, "status": "following_up"}
            )
            return bool(saved)

        self.assertEqual(sorted(self.parallel(update)), [False, True])
        self.assertEqual(Escalation.objects.get(pk=lead_id).version, 2)
