import json
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.forms.models import model_to_dict
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from outreach import crm, legacy_handoffs
from outreach.crm_forms import HandoffForm, LeadUpdateForm, LegacyRoutingForm, SubmissionForm
from outreach.forms import ProspectForm, UserProvisionForm
from outreach.models import (
    CRMActivity, Escalation, FounderCAEAssignment, Outreach, Profile, Prospect, ProspectUpdateAudit,
)
from outreach.tests.test_company_api import CompanyApiTestMixin


class LegacyHandoffTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("admin@handoff.test", Profile.Role.SYSTEM_ADMIN)
        self.operator = self.make_user("operator@handoff.test", Profile.Role.LINKEDIN_OUTREACH)
        self.manager = self.make_user("manager@handoff.test", Profile.Role.MANAGER)
        self.cae = self.make_user("cae@handoff.test", Profile.Role.ACCOUNT_EXECUTIVE, "Confirmed CAE")
        self.route = FounderCAEAssignment.objects.create(
            founder_account=Prospect.FounderAccount.KANDARP_SONI,
            executive=self.cae, updated_by=self.admin,
        )
        self.prospect = self.make_prospect(
            self.operator, "Legacy company", "https://legacy.example.com",
            founder_account=self.route.founder_account,
            contact_linkedin_url="https://linkedin.com/in/legacy",
            linkedin_connection_status=Prospect.LinkedInConnectionStatus.ACCEPTED,
            personalization_note="Operations practice",
            interest_signal="Asked for a founder call",
        )

    def flag(self):
        self.prospect.founder_escalation_required = True
        self.prospect.save()

    def outreach_data(self, activity=Outreach.ActivityType.FOUNDER_ESCALATION):
        return {
            "activity_type": activity, "medium": "linkedin",
            "outreach_date": timezone.localdate().isoformat(), "response": "Client requested discussion",
        }

    def api(self, method, route, pk, data, user=None):
        return self.client.generic(
            method, reverse(route, args=[pk]), data=json.dumps(data),
            content_type="application/json", HTTP_AUTHORIZATION=self.bearer(user or self.operator),
        )

    def test_every_channel_accepts_only_active_cae_in_all_assignment_forms(self):
        inactive = self.make_user("inactive@handoff.test", Profile.Role.ACCOUNT_EXECUTIVE)
        inactive.is_active = False
        inactive.save()
        multi = self.make_user("multi@handoff.test", Profile.Role.MANAGER)
        multi.profile.set_roles([Profile.Role.MANAGER, Profile.Role.ACCOUNT_EXECUTIVE])
        for channel in Prospect.Workstream.values:
            with self.subTest(channel=channel):
                prospect = self.make_prospect(self.operator, channel, f"https://{channel}.test", workstream=channel)
                for allowed in (self.cae, multi):
                    form = HandoffForm({"assigned_to": allowed.pk, "context": "Ready", "response_confirmed": True}, prospect=prospect)
                    self.assertTrue(form.is_valid(), form.errors)
                for rejected in (self.manager, self.operator, inactive):
                    data = {"assigned_to": rejected.pk, "context": "Ready", "response_confirmed": True}
                    for historical in (False, True):
                        form = HandoffForm(data, prospect=prospect, historical=historical)
                        self.assertFalse(form.is_valid())
                        self.assertIn("assigned_to", form.errors)
                    lead = Escalation(prospect=prospect, assigned_to=rejected, context="Ready", source_channel=channel)
                    with self.assertRaises(ValidationError):
                        lead.full_clean()
        form = SubmissionForm(user=self.manager)
        self.assertSetEqual(set(form.fields["assigned_to"].queryset), {self.cae, multi})
        form = LegacyRoutingForm()
        self.assertSetEqual(set(form.fields[self.route.founder_account].queryset), {self.cae, multi})

    def test_crm_api_rejects_manager_recipient_for_every_channel(self):
        for channel in Prospect.Workstream.values:
            prospect = self.make_prospect(self.operator, channel, f"https://api-{channel}.test", workstream=channel)
            response = self.api("POST", "api_crm_handoff", prospect.pk, {
                "assigned_to": self.manager.pk, "context": "Ready", "response_confirmed": True,
            }, user=self.manager)
            self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Escalation.objects.exists())

    def test_inactive_cae_cannot_be_selected_even_as_current_assignment(self):
        self.flag()
        lead, _ = legacy_handoffs.ensure_handoff(self.prospect, self.operator)
        self.cae.is_active = False
        self.cae.save()
        form = LeadUpdateForm(instance=lead, user=self.manager)
        self.assertNotIn(self.cae, form.fields["assigned_to"].queryset)
        with self.assertRaises(ValidationError):
            legacy_handoffs.ensure_handoff(self.prospect, self.operator)

    def test_legacy_web_outreach_creates_one_cae_lead(self):
        self.client.force_login(self.operator)
        response = self.client.post(reverse("outreach_add", args=[self.prospect.pk]), self.outreach_data())
        self.assertEqual(response.status_code, 302)
        lead = Escalation.objects.get()
        self.assertEqual(lead.assigned_to, self.cae)
        self.assertEqual(lead.escalated_by, self.operator)
        self.assertFalse(lead.historical)
        response = self.client.post(reverse("outreach_add", args=[self.prospect.pk]), self.outreach_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Escalation.objects.count(), 1)
        self.assertEqual(CRMActivity.objects.count(), 1)

    def test_api_outreach_and_meeting_handoff_bridge(self):
        for activity in legacy_handoffs.ESCALATION_ACTIVITIES:
            response = self.api("POST", "api_prospect_outreach_list", self.prospect.pk, self.outreach_data(activity))
            self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Escalation.objects.count(), 1)

    def test_api_edit_outreach_bridges_with_actual_editor(self):
        outreach = Outreach.objects.create(
            prospect=self.prospect, recorded_by=self.operator, sequence_number=1,
            **self.outreach_data(Outreach.ActivityType.INBOUND_RESPONSE),
        )
        response = self.api("PATCH", "api_outreach_detail", outreach.pk,
                            {"activity_type": "founder_escalation"}, user=self.manager)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Escalation.objects.get().escalated_by, self.manager)

    def test_web_edit_outreach_bridges(self):
        outreach = Outreach.objects.create(
            prospect=self.prospect, recorded_by=self.operator, sequence_number=1,
            **self.outreach_data(Outreach.ActivityType.INBOUND_RESPONSE),
        )
        self.client.force_login(self.operator)
        response = self.client.post(reverse("outreach_update", args=[outreach.pk]), self.outreach_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Escalation.objects.get().assigned_to, self.cae)

    def test_prospect_flag_form_creates_queue_record(self):
        form = ProspectForm({
            **model_to_dict(self.prospect, fields=ProspectForm.Meta.fields),
            "founder_escalation_required": True,
        }, instance=self.prospect, user=self.operator)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(Escalation.objects.get().assigned_to, self.cae)

    def test_missing_or_ineligible_route_blocks_flag_and_outreach_without_partial_writes(self):
        for recipient in (None, self.manager):
            if recipient is None:
                self.route.delete()
            else:
                FounderCAEAssignment.objects.create(founder_account=self.prospect.founder_account, executive=recipient, updated_by=self.admin)
            response = self.api("PATCH", "api_prospect_detail", self.prospect.pk, {"founder_escalation_required": True})
            self.assertEqual(response.status_code, 400)
            response = self.api("POST", "api_prospect_outreach_list", self.prospect.pk, self.outreach_data())
            self.assertEqual(response.status_code, 400)
            self.prospect.refresh_from_db()
            self.assertFalse(self.prospect.founder_escalation_required)
            self.assertEqual(ProspectUpdateAudit.objects.count(), 0)
            self.assertEqual(Outreach.objects.count(), 0)
            self.assertEqual(Escalation.objects.count(), 0)

    def test_inactive_route_is_not_a_valid_handoff(self):
        self.cae.is_active = False
        self.cae.save()
        response = self.api("POST", "api_prospect_outreach_list", self.prospect.pk, self.outreach_data())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Outreach.objects.exists())

    def test_unrelated_edit_to_unmapped_historical_flag_does_not_assign_or_block(self):
        self.flag()
        self.route.delete()
        response = self.api("PATCH", "api_interest_handoff", self.prospect.pk, {"interest_signal": "Corrected old reply"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(Escalation.objects.exists())

    def test_invalid_contact_and_unqualified_lead_are_not_backfilled(self):
        self.flag()
        self.prospect.is_not_eligible = True
        self.prospect.save()
        self.assertEqual(legacy_handoffs.backfill(self.admin, [(self.prospect.pk, self.cae.pk)]), (0, 1))
        self.prospect.is_not_eligible = False
        self.prospect.contact_name = ""
        self.prospect.save()
        self.assertEqual(legacy_handoffs.backfill(self.admin, [(self.prospect.pk, self.cae.pk)]), (0, 1))

    def test_late_bridge_failure_rolls_back_outreach_and_prospect_changes(self):
        with patch("outreach.workflows.ensure_handoff", side_effect=ValidationError("Route changed")):
            response = self.api("POST", "api_prospect_outreach_list", self.prospect.pk, self.outreach_data())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Outreach.objects.exists())
        self.prospect.refresh_from_db()
        self.assertFalse(self.prospect.founder_escalation_required)
        self.assertEqual(self.prospect.stage, Prospect.Stage.ELIGIBLE)

    def test_backfill_preserves_original_records_and_unknown_history(self):
        self.flag()
        before = model_to_dict(self.prospect)
        targets = [(self.prospect.pk, self.cae.pk)]
        self.assertEqual(legacy_handoffs.backfill(self.admin, targets), (1, 0))
        self.assertEqual(legacy_handoffs.backfill(self.admin, targets), (0, 1))
        lead = Escalation.objects.get()
        self.assertTrue(lead.historical)
        self.assertIsNone(lead.escalated_at)
        self.assertIsNone(lead.escalated_by)
        self.assertEqual(lead.activities.get().actor, self.admin)
        self.prospect.refresh_from_db()
        self.assertEqual(model_to_dict(self.prospect), before)
        response = self.api("GET", "api_crm_lead_detail", lead.pk, {}, user=self.cae)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(b'"escalated_by": null', response.content)

    def test_explicit_date_only_history_is_preserved_without_inventing_time(self):
        day = timezone.localdate() - timedelta(days=60)
        outreach = Outreach.objects.create(
            prospect=self.prospect, recorded_by=self.operator, sequence_number=1,
            activity_type="founder_escalation", medium="linkedin", outreach_date=day, response="Old reply",
        )
        before = model_to_dict(outreach)
        self.assertEqual(legacy_handoffs.backfill(self.admin, [(self.prospect.pk, self.cae.pk)]), (1, 0))
        lead = Escalation.objects.get()
        self.assertIsNone(lead.escalated_at)
        self.assertIn(str(day), lead.context)
        self.assertIn("Old reply", lead.context)
        self.assertEqual(lead.escalated_by, self.operator)
        outreach.refresh_from_db()
        self.assertEqual(model_to_dict(outreach), before)

    def test_flag_transition_audit_supplies_known_actor_and_timestamp(self):
        self.flag()
        audit = ProspectUpdateAudit.objects.create(
            prospect=self.prospect, modified_by=self.operator,
            previous_values={"founder_escalation_required": False},
            changed_values={"founder_escalation_required": True},
        )
        legacy_handoffs.backfill(self.admin, [(self.prospect.pk, self.cae.pk)])
        lead = Escalation.objects.get()
        self.assertEqual(lead.escalated_at, audit.created_at)
        self.assertEqual(lead.escalated_by, self.operator)

    def test_missing_recipient_stays_outside_queue_for_user_review(self):
        self.flag()
        self.prospect.founder_account = ""
        self.prospect.save()
        row = legacy_handoffs.preview()[0]
        self.assertIsNone(row["executive"])
        self.assertTrue(row["reason"])
        self.assertEqual(legacy_handoffs.backfill(self.admin, [(self.prospect.pk, self.cae.pk)]), (0, 1))
        self.assertFalse(Escalation.objects.exists())

    def test_interested_only_records_are_never_auto_imported(self):
        self.prospect.stage = Prospect.Stage.INTERESTED
        self.prospect.save()
        self.assertFalse(legacy_handoffs.candidates().exists())
        self.assertEqual(legacy_handoffs.backfill(self.admin, [(self.prospect.pk, self.cae.pk)]), (0, 1))

    def test_all_channel_historical_escalations_require_cae_mapping(self):
        for channel in Prospect.Workstream.values:
            prospect = self.make_prospect(self.operator, channel, f"https://historical-{channel}.test",
                                          workstream=channel, founder_escalation_required=True)
            self.assertEqual(legacy_handoffs.backfill(self.admin, [(prospect.pk, self.manager.pk)]), (0, 1))
            form, lead = crm.escalate_prospect(self.admin, prospect.pk,
                {"assigned_to": self.cae.pk, "context": "User confirmed recipient"}, historical=True)
            self.assertIsNotNone(lead, form.errors)
            self.assertEqual(lead.assigned_to, self.cae)

    def test_existing_assignment_and_status_are_never_overwritten(self):
        self.flag()
        lead, _ = legacy_handoffs.ensure_handoff(self.prospect, self.operator)
        lead.status = Escalation.Status.FOLLOWING_UP
        lead.save()
        before = model_to_dict(lead)
        self.route.executive = self.make_user("other@handoff.test", Profile.Role.ACCOUNT_EXECUTIVE)
        self.route.save()
        same, created = legacy_handoffs.ensure_handoff(self.prospect, self.admin, historical=True)
        self.assertFalse(created)
        self.assertEqual(model_to_dict(same), before)

    def test_route_change_after_preview_requires_new_review(self):
        self.flag()
        old_target = [(self.prospect.pk, self.cae.pk)]
        self.route.executive = self.make_user("other@handoff.test", Profile.Role.ACCOUNT_EXECUTIVE)
        self.route.save()
        self.assertEqual(legacy_handoffs.backfill(self.admin, old_target), (0, 1))

    def test_preview_is_read_only_and_backfill_requires_signed_confirmed_admin_post(self):
        self.flag()
        url = reverse("crm_legacy_routing")
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["ready_count"], 1)
        self.assertFalse(Escalation.objects.exists())
        token = response.context["preview_token"]
        for data in ({"preview": token}, {"preview": token + "invalid", "confirmed": "yes"}):
            self.client.post(url, {"action": "backfill", **data})
            self.assertFalse(Escalation.objects.exists())
        self.client.post(url, {"action": "backfill", "preview": token, "confirmed": "yes"})
        self.assertEqual(Escalation.objects.count(), 1)
        self.client.post(url, {"action": "backfill", "preview": token, "confirmed": "yes"})
        self.assertEqual(Escalation.objects.count(), 1)

    def test_backfill_and_routing_reject_non_admin_and_require_csrf(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("crm_legacy_routing")).status_code, 403)
        with self.assertRaises(PermissionDenied):
            legacy_handoffs.backfill(self.manager, [])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(reverse("crm_legacy_routing"), {"action": "save_routes"}).status_code, 403)

    def test_routing_preserves_roles_and_prevents_removing_mapped_cae_role(self):
        before = self.manager.profile.assigned_roles
        form = LegacyRoutingForm({self.route.founder_account: self.manager.pk})
        self.assertFalse(form.is_valid())
        self.manager.profile.refresh_from_db()
        self.assertEqual(self.manager.profile.assigned_roles, before)
        form = UserProvisionForm({"email": self.cae.email, "full_name": "Confirmed CAE", "roles": ["manager"]}, instance=self.cae)
        self.assertFalse(form.is_valid())
        self.assertIn("roles", form.errors)
