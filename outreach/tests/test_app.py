from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from outreach.adapters import FlexGCCSocialAccountAdapter
from outreach.forms import OutreachForm, ProspectForm
from outreach.models import Outreach, Profile, Prospect


class AppTestMixin:
    def make_user(self, email, role=Profile.Role.INTERN, name="Test User"):
        first_name, _, last_name = name.partition(" ")
        user = get_user_model().objects.create_user(
            username=email,
            email=email,
            first_name=first_name,
            last_name=last_name,
            password="not-used-in-production",
        )
        user.profile.role = role
        user.profile.save(update_fields=["role"])
        return user

    def make_prospect(self, owner, company="Acme Health", **overrides):
        owner_role = owner.profile.role
        values = {
            "owner": owner,
            "created_by": overrides.pop("created_by", owner),
            "workstream": owner_role if owner_role in Prospect.Workstream.values else Prospect.Workstream.INTERN,
            "company_name": company,
            "website": "https://example.com",
            "short_description": "Healthcare operations platform.",
            "location": "Chicago, Illinois",
            "consulting_focus": "Operations consulting",
            "client_segment": "Small and mid-sized businesses",
            "eligibility_evidence": "Independent consultancy serving mid-market clients.",
            "contact_name": "Asha Rao",
            "contact_title": "Managing Partner",
            "contact_email": "asha@example.com",
            "status": Prospect.Status.NOT_RESPONDED,
        }
        values.update(overrides)
        return Prospect.objects.create(**values)

    def prospect_form_data(self, **overrides):
        values = {
            "workstream": Prospect.Workstream.INTERN,
            "company_name": "New Advisory",
            "website": "https://new-advisory.example.com",
            "short_description": "Boutique operations consultancy.",
            "location": "Chicago, Illinois",
            "consulting_focus": "Operations and growth advisory",
            "client_segment": "Small and mid-sized businesses",
            "eligibility_evidence": "Founder-led firm with published mid-market case studies.",
            "contact_name": "Asha Rao",
            "contact_title": "Managing Partner",
            "contact_email": "asha@new-advisory.example.com",
            "status": Prospect.Status.NOT_RESPONDED,
            "material_shared": "",
        }
        values.update(overrides)
        return values


class ProspectValidationTests(AppTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user("intern@example.com")

    def test_meeting_scheduled_requires_date_and_time(self):
        prospect = self.make_prospect(self.intern, status=Prospect.Status.MEETING_SCHEDULED)
        with self.assertRaises(ValidationError) as error:
            prospect.full_clean()
        self.assertIn("meeting_scheduled_at", error.exception.message_dict)

    def test_contact_method_is_required(self):
        form = ProspectForm(
            data={
                "company_name": "No Contact Ltd",
                "website": "https://example.com",
                "short_description": "Description",
                "contact_name": "Person",
                "status": Prospect.Status.NOT_RESPONDED,
            },
            user=self.intern,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("contact_email", form.errors)

    def test_next_action_and_date_must_be_set_together(self):
        form = ProspectForm(
            data={
                "company_name": "Action Ltd",
                "website": "https://example.com",
                "short_description": "Description",
                "contact_name": "Person",
                "contact_email": "person@example.com",
                "next_action": "Send deck",
                "status": Prospect.Status.NOT_RESPONDED,
            },
            user=self.intern,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("next_action_date", form.errors)

    def test_intern_entry_requires_playbook_qualification_fields(self):
        form = ProspectForm(
            data={
                "workstream": Prospect.Workstream.INTERN,
                "company_name": "Incomplete Advisory",
                "website": "https://incomplete.example.com",
                "short_description": "Description",
                "contact_name": "Person",
                "contact_email": "person@incomplete.example.com",
                "status": Prospect.Status.NOT_RESPONDED,
            },
            user=self.intern,
        )
        self.assertFalse(form.is_valid())
        for field_name in [
            "location",
            "consulting_focus",
            "client_segment",
            "eligibility_evidence",
            "contact_title",
        ]:
            self.assertIn(field_name, form.errors)


class DashboardPermissionTests(AppTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user("intern@example.com", name="Isha Intern")
        self.other_intern = self.make_user("other@example.com", name="Omar Intern")
        self.manager = self.make_user("manager@example.com", Profile.Role.MANAGER, "Meera Manager")
        self.admin = self.make_user("admin@example.com", Profile.Role.SYSTEM_ADMIN, "Sana Admin")
        self.own = self.make_prospect(self.intern, "Own Company")
        self.other = self.make_prospect(self.other_intern, "Other Company")

    def test_intern_sees_only_assigned_prospects(self):
        self.client.force_login(self.intern)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Own Company")
        self.assertNotContains(response, "Other Company")

    def test_manager_sees_all_intern_prospects(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Own Company")
        self.assertContains(response, "Other Company")

    def test_system_admin_has_manager_level_pipeline_visibility(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Own Company")
        self.assertContains(response, "Other Company")

    def test_intern_cannot_open_another_interns_prospect(self):
        self.client.force_login(self.intern)
        response = self.client.get(reverse("prospect_detail", args=[self.other.pk]))
        self.assertEqual(response.status_code, 404)

    def test_dashboard_filters_today_status_and_outreach_count(self):
        today = timezone.localdate()
        self.own.next_action = "Call today"
        self.own.next_action_date = today
        self.own.status = Prospect.Status.MEETING_TO_SCHEDULE
        self.own.save()
        Outreach.objects.create(
            prospect=self.own,
            sequence_number=1,
            medium=Outreach.Medium.EMAIL,
            outreach_date=today,
            recorded_by=self.intern,
        )
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("dashboard"),
            {"due": "today", "status": Prospect.Status.MEETING_TO_SCHEDULE, "outreach_count": "1"},
        )
        self.assertContains(response, "Own Company")
        self.assertNotContains(response, "Other Company")

    def test_dashboard_paginates_at_twenty(self):
        for number in range(20):
            self.make_prospect(self.intern, f"Company {number:02}")
        self.client.force_login(self.intern)
        first_page = self.client.get(reverse("dashboard"))
        second_page = self.client.get(reverse("dashboard"), {"page": 2})
        self.assertEqual(len(first_page.context["page_obj"]), 20)
        self.assertEqual(len(second_page.context["page_obj"]), 1)


class MultiRoleWorkflowTests(AppTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user("intern@example.com", name="Isha Intern")
        self.inside_sales = self.make_user(
            "inside@example.com", Profile.Role.INSIDE_SALES, "Ivan Inside Sales"
        )
        self.linkedin_operator = self.make_user(
            "linkedin@example.com", Profile.Role.LINKEDIN_OUTREACH, "Leena LinkedIn"
        )
        self.manager = self.make_user("manager@example.com", Profile.Role.MANAGER, "Meera Manager")

    def make_linkedin_prospect(self):
        return self.make_prospect(
            self.linkedin_operator,
            "LinkedIn Advisory",
            website="https://linkedin-advisory.example.com",
            workstream=Prospect.Workstream.LINKEDIN_OUTREACH,
            contact_linkedin_url="https://www.linkedin.com/in/asha-rao",
            founder_account=Prospect.FounderAccount.KANDARP_SONI,
            linkedin_connection_status=Prospect.LinkedInConnectionStatus.NOT_SENT,
            personalization_note="Chicago operations practice.",
        )

    def test_inside_sales_user_can_create_own_workstream_record(self):
        self.client.force_login(self.inside_sales)
        response = self.client.post(
            reverse("prospect_create"),
            self.prospect_form_data(
                workstream=Prospect.Workstream.INSIDE_SALES,
                company_name="Inside Sales Advisory",
                website="https://inside-sales-advisory.example.com",
                contact_email="partner@inside-sales-advisory.example.com",
            ),
        )
        prospect = Prospect.objects.get(company_name="Inside Sales Advisory")
        self.assertRedirects(response, prospect.get_absolute_url())
        self.assertEqual(prospect.owner, self.inside_sales)
        self.assertEqual(prospect.workstream, Prospect.Workstream.INSIDE_SALES)
        self.assertEqual(prospect.stage, Prospect.Stage.ELIGIBLE)

    def test_linkedin_workstream_requires_founder_account_and_personalization(self):
        form = ProspectForm(
            data=self.prospect_form_data(
                workstream=Prospect.Workstream.LINKEDIN_OUTREACH,
                contact_email="",
                contact_linkedin_url="https://www.linkedin.com/in/prospect",
            ),
            user=self.linkedin_operator,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("founder_account", form.errors)
        self.assertIn("linkedin_connection_status", form.errors)
        self.assertIn("personalization_note", form.errors)

    def test_manager_must_match_owner_class_to_workstream(self):
        form = ProspectForm(
            data=self.prospect_form_data(
                workstream=Prospect.Workstream.INSIDE_SALES,
                owner=self.intern.pk,
            ),
            user=self.manager,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("owner", form.errors)

    def test_same_company_can_have_separate_workstream_histories(self):
        self.make_prospect(self.intern, "Existing Advisory", website="https://advisory.example.com")
        form = ProspectForm(
            data=self.prospect_form_data(
                workstream=Prospect.Workstream.INSIDE_SALES,
                website="https://www.advisory.example.com/about",
            ),
            user=self.inside_sales,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_duplicate_company_domain_is_blocked_within_a_workstream(self):
        self.make_prospect(
            self.inside_sales,
            "Existing Advisory",
            website="https://advisory.example.com",
        )
        form = ProspectForm(
            data=self.prospect_form_data(
                workstream=Prospect.Workstream.INSIDE_SALES,
                website="https://www.advisory.example.com/about",
            ),
            user=self.inside_sales,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Potential duplicate", form.non_field_errors()[0])

    def test_linkedin_activity_updates_connection_and_playbook_stage(self):
        prospect = self.make_linkedin_prospect()
        self.client.force_login(self.linkedin_operator)
        response = self.client.post(
            reverse("outreach_add", args=[prospect.pk]),
            {
                "activity_type": Outreach.ActivityType.CONNECTION_REQUEST,
                "medium": Outreach.Medium.LINKEDIN,
                "outreach_date": timezone.localdate().isoformat(),
            },
        )
        self.assertRedirects(response, prospect.get_absolute_url())
        prospect.refresh_from_db()
        self.assertEqual(prospect.stage, Prospect.Stage.CONTACTED)
        self.assertEqual(
            prospect.linkedin_connection_status,
            Prospect.LinkedInConnectionStatus.REQUEST_SENT,
        )

        response = self.client.post(
            reverse("outreach_add", args=[prospect.pk]),
            {
                "activity_type": Outreach.ActivityType.CONNECTION_ACCEPTED,
                "medium": Outreach.Medium.LINKEDIN,
                "outreach_date": timezone.localdate().isoformat(),
            },
        )
        self.assertRedirects(response, prospect.get_absolute_url())
        prospect.refresh_from_db()
        self.assertEqual(
            prospect.linkedin_connection_status,
            Prospect.LinkedInConnectionStatus.ACCEPTED,
        )

        response = self.client.post(
            reverse("outreach_add", args=[prospect.pk]),
            {
                "activity_type": Outreach.ActivityType.INBOUND_RESPONSE,
                "medium": Outreach.Medium.LINKEDIN,
                "outreach_date": timezone.localdate().isoformat(),
                "response": "Interested in a founder conversation.",
            },
        )
        self.assertRedirects(response, prospect.get_absolute_url())
        prospect.refresh_from_db()
        self.assertEqual(prospect.stage, Prospect.Stage.RESPONDED)

    def test_linkedin_outreach_form_locks_medium_to_linkedin(self):
        prospect = self.make_linkedin_prospect()
        form = OutreachForm(
            data={
                "activity_type": Outreach.ActivityType.CONNECTION_REQUEST,
                "medium": Outreach.Medium.PHONE,
                "outreach_date": timezone.localdate().isoformat(),
            },
            prospect=prospect,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["medium"], Outreach.Medium.LINKEDIN)

    def test_linkedin_form_defaults_to_the_next_valid_connection_activity(self):
        prospect = self.make_linkedin_prospect()
        self.assertEqual(
            OutreachForm(prospect=prospect)["activity_type"].value(),
            Outreach.ActivityType.CONNECTION_REQUEST,
        )

        prospect.linkedin_connection_status = Prospect.LinkedInConnectionStatus.REQUEST_SENT
        prospect.save(update_fields=["linkedin_connection_status"])
        self.assertEqual(
            OutreachForm(prospect=prospect)["activity_type"].value(),
            Outreach.ActivityType.CONNECTION_ACCEPTED,
        )

    def test_editing_old_connection_request_does_not_regress_accepted_state(self):
        prospect = self.make_linkedin_prospect()
        outreach = Outreach.objects.create(
            prospect=prospect,
            sequence_number=1,
            activity_type=Outreach.ActivityType.CONNECTION_REQUEST,
            medium=Outreach.Medium.LINKEDIN,
            outreach_date=timezone.localdate(),
            recorded_by=self.linkedin_operator,
        )
        prospect.linkedin_connection_status = Prospect.LinkedInConnectionStatus.ACCEPTED
        prospect.stage = Prospect.Stage.CONTACTED
        prospect.save(update_fields=["linkedin_connection_status", "stage"])

        self.client.force_login(self.linkedin_operator)
        response = self.client.post(
            reverse("outreach_update", args=[outreach.pk]),
            {
                "activity_type": Outreach.ActivityType.CONNECTION_REQUEST,
                "medium": Outreach.Medium.LINKEDIN,
                "outreach_date": timezone.localdate().isoformat(),
                "response": "Corrected historical note.",
            },
        )

        self.assertRedirects(response, prospect.get_absolute_url())
        prospect.refresh_from_db()
        self.assertEqual(
            prospect.linkedin_connection_status,
            Prospect.LinkedInConnectionStatus.ACCEPTED,
        )

    def test_linkedin_post_acceptance_message_is_blocked_until_connection_is_accepted(self):
        prospect = self.make_linkedin_prospect()
        form = OutreachForm(
            data={
                "activity_type": Outreach.ActivityType.INITIAL_OUTREACH,
                "medium": Outreach.Medium.LINKEDIN,
                "outreach_date": timezone.localdate().isoformat(),
            },
            prospect=prospect,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("accepted", form.errors["activity_type"][0])

    def test_manager_filters_dashboard_by_user_class(self):
        inside = self.make_prospect(
            self.inside_sales,
            "Inside Prospect",
            website="https://inside.example.com",
        )
        linkedin = self.make_linkedin_prospect()
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("dashboard"),
            {"workstream": Prospect.Workstream.INSIDE_SALES},
        )
        self.assertContains(response, inside.company_name)
        self.assertNotContains(response, linkedin.company_name)

    def test_manager_sees_all_operator_classes_and_frontline_users_remain_scoped(self):
        inside = self.make_prospect(
            self.inside_sales,
            "Inside Visibility",
            website="https://inside-visibility.example.com",
        )
        linkedin = self.make_linkedin_prospect()
        self.client.force_login(self.manager)
        manager_dashboard = self.client.get(reverse("dashboard"))
        self.assertContains(manager_dashboard, inside.company_name)
        self.assertContains(manager_dashboard, linkedin.company_name)

        self.client.force_login(self.inside_sales)
        self.assertEqual(
            self.client.get(reverse("prospect_detail", args=[linkedin.pk])).status_code,
            404,
        )

    def test_scheduled_meeting_requires_timezone_and_participants(self):
        form = ProspectForm(
            data=self.prospect_form_data(
                status=Prospect.Status.MEETING_SCHEDULED,
                meeting_scheduled_at="2026-08-01T10:00",
            ),
            user=self.intern,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("meeting_timezone", form.errors)
        self.assertIn("meeting_participants", form.errors)


class UnassignedResearchQueueTests(AppTestMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("admin@example.com", Profile.Role.SYSTEM_ADMIN, "Sana Admin")
        self.intern = self.make_user("intern@example.com", Profile.Role.INTERN, "Isha Intern")
        self.other_intern = self.make_user("other@example.com", Profile.Role.INTERN, "Omar Intern")
        self.inside_sales = self.make_user(
            "inside@example.com",
            Profile.Role.INSIDE_SALES,
            "Ivan Inside Sales",
        )
        self.prospect = Prospect.objects.create(
            owner=None,
            workstream=Prospect.Workstream.INTERN,
            stage=Prospect.Stage.RESEARCH,
            company_name="Imported Advisory",
            website="https://imported-advisory.example.com",
            location="Miami, FL",
            import_source="Florida and Chicago target firms",
            import_key="imported-advisory.example.com",
            created_by=self.admin,
        )

    def test_research_record_allows_missing_contact_and_qualification_data(self):
        self.prospect.full_clean()

    def test_frontline_user_sees_only_the_unassigned_queue_for_their_role(self):
        self.client.force_login(self.intern)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, self.prospect.company_name)
        self.assertContains(response, "Research required")

        self.client.force_login(self.inside_sales)
        self.assertNotContains(self.client.get(reverse("dashboard")), self.prospect.company_name)

    def test_claim_is_atomic_and_removes_record_from_other_users_queue(self):
        self.client.force_login(self.intern)
        response = self.client.post(reverse("prospect_claim", args=[self.prospect.pk]))
        self.assertRedirects(response, self.prospect.get_absolute_url())
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.owner, self.intern)

        self.client.force_login(self.other_intern)
        self.assertEqual(
            self.client.get(reverse("prospect_detail", args=[self.prospect.pk])).status_code,
            404,
        )

    def test_research_must_be_completed_before_outreach(self):
        self.prospect.owner = self.intern
        self.prospect.save(update_fields=["owner"])
        self.client.force_login(self.intern)
        response = self.client.post(
            reverse("outreach_add", args=[self.prospect.pk]),
            {
                "activity_type": Outreach.ActivityType.INITIAL_OUTREACH,
                "medium": Outreach.Medium.EMAIL,
                "outreach_date": timezone.localdate().isoformat(),
            },
        )
        self.assertRedirects(response, self.prospect.get_absolute_url())
        self.assertEqual(self.prospect.outreaches.count(), 0)

    def test_completed_research_moves_record_to_eligible(self):
        self.prospect.owner = self.intern
        self.prospect.save(update_fields=["owner"])
        self.client.force_login(self.intern)
        response = self.client.post(
            reverse("prospect_update", args=[self.prospect.pk]),
            self.prospect_form_data(
                company_name=self.prospect.company_name,
                website=self.prospect.website,
            ),
        )
        self.assertRedirects(response, self.prospect.get_absolute_url())
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.stage, Prospect.Stage.ELIGIBLE)


class TargetFirmImportTests(AppTestMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("admin@example.com", Profile.Role.SYSTEM_ADMIN, "Sana Admin")

    def test_import_is_validated_idempotent_and_populates_all_role_queues(self):
        dry_run_output = StringIO()
        call_command("import_target_firms", stdout=dry_run_output)
        self.assertIn("Target-firm import DRY RUN", dry_run_output.getvalue())
        self.assertEqual(Prospect.objects.count(), 0)

        apply_output = StringIO()
        call_command("import_target_firms", apply=True, stdout=apply_output)
        self.assertIn("Source rows validated: 170", apply_output.getvalue())
        self.assertEqual(Prospect.objects.count(), 340)
        self.assertEqual(
            Prospect.objects.filter(workstream=Prospect.Workstream.INTERN).count(),
            80,
        )
        self.assertEqual(
            Prospect.objects.filter(workstream=Prospect.Workstream.INSIDE_SALES).count(),
            90,
        )
        self.assertEqual(
            Prospect.objects.filter(workstream=Prospect.Workstream.LINKEDIN_OUTREACH).count(),
            170,
        )
        self.assertEqual(Prospect.objects.filter(stage=Prospect.Stage.RESEARCH).count(), 340)
        self.assertEqual(Prospect.objects.filter(owner__isnull=True).count(), 340)
        self.assertEqual(Prospect.objects.values("company_id").distinct().count(), 170)
        sample_domain = Prospect.objects.filter(import_key="360alignmentadvisors.com")
        self.assertEqual(sample_domain.values("company_id").distinct().count(), 1)

        second_output = StringIO()
        call_command("import_target_firms", apply=True, stdout=second_output)
        self.assertEqual(Prospect.objects.count(), 340)
        self.assertIn("created=0", second_output.getvalue())


class OutreachWorkflowTests(AppTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user("intern@example.com")
        self.prospect = self.make_prospect(self.intern)
        self.client.force_login(self.intern)

    def test_records_no_more_than_five_outreaches(self):
        today = timezone.localdate()
        for number in range(1, 6):
            Outreach.objects.create(
                prospect=self.prospect,
                sequence_number=number,
                medium=Outreach.Medium.EMAIL,
                outreach_date=today,
                recorded_by=self.intern,
            )
        response = self.client.post(
            reverse("outreach_add", args=[self.prospect.pk]),
            {"medium": Outreach.Medium.EMAIL, "outreach_date": today.isoformat(), "response": "Sixth"},
        )
        self.assertRedirects(response, self.prospect.get_absolute_url())
        self.assertEqual(self.prospect.outreaches.count(), 5)

    def test_medium_requires_matching_contact_detail(self):
        response = self.client.post(
            reverse("outreach_add", args=[self.prospect.pk]),
            {"medium": Outreach.Medium.PHONE, "outreach_date": timezone.localdate().isoformat()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Add the contact phone number", status_code=400)
        self.assertEqual(self.prospect.outreaches.count(), 0)

    def test_future_outreach_date_is_rejected(self):
        response = self.client.post(
            reverse("outreach_add", args=[self.prospect.pk]),
            {
                "medium": Outreach.Medium.EMAIL,
                "outreach_date": (timezone.localdate() + timedelta(days=1)).isoformat(),
            },
        )
        self.assertContains(response, "Outreach date cannot be in the future", status_code=400)


class SystemAdminTests(AppTestMixin, TestCase):
    def setUp(self):
        self.admin = self.make_user("admin@example.com", Profile.Role.SYSTEM_ADMIN, "Sana Admin")
        self.intern = self.make_user("intern@example.com", Profile.Role.INTERN, "Isha Intern")

    def test_only_system_admin_can_view_user_management(self):
        self.client.force_login(self.intern)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("user_list")).status_code, 200)

    def test_admin_can_preprovision_google_user_and_role(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("user_create"),
            {"full_name": "Maya Manager", "email": "maya@example.com", "role": Profile.Role.MANAGER},
        )
        self.assertRedirects(response, reverse("user_list"))
        user = get_user_model().objects.get(email="maya@example.com")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.profile.role, Profile.Role.MANAGER)
        self.assertTrue(EmailAddress.objects.filter(user=user, email="maya@example.com", primary=True).exists())

    def test_admin_can_create_both_new_outreach_user_classes(self):
        self.client.force_login(self.admin)
        for role, email, name in [
            (Profile.Role.INSIDE_SALES, "inside@example.com", "Ivan Inside"),
            (Profile.Role.LINKEDIN_OUTREACH, "linkedin@example.com", "Leena LinkedIn"),
        ]:
            with self.subTest(role=role):
                response = self.client.post(
                    reverse("user_create"),
                    {"full_name": name, "email": email, "role": role},
                )
                self.assertRedirects(response, reverse("user_list"))
                self.assertEqual(get_user_model().objects.get(email=email).profile.role, role)

    def test_delete_unlinked_user_removes_account(self):
        target = self.make_user("unused@example.com")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_delete", args=[target.pk]))
        self.assertRedirects(response, reverse("user_list"))
        self.assertFalse(get_user_model().objects.filter(pk=target.pk).exists())

    def test_delete_user_with_sales_history_deactivates_account(self):
        self.make_prospect(self.intern)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_delete", args=[self.intern.pk]))
        self.assertRedirects(response, reverse("user_list"))
        self.intern.refresh_from_db()
        self.assertFalse(self.intern.is_active)
        self.assertTrue(Prospect.objects.filter(owner=self.intern).exists())

    def test_changing_email_disconnects_old_google_identity(self):
        EmailAddress.objects.create(user=self.intern, email=self.intern.email, primary=True, verified=True)
        SocialAccount.objects.create(user=self.intern, provider="google", uid="old-google-identity")
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("user_update", args=[self.intern.pk]),
            {"full_name": "Isha Intern", "email": "isha.new@example.com", "role": Profile.Role.INTERN},
        )
        self.assertRedirects(response, reverse("user_list"))
        self.intern.refresh_from_db()
        self.assertEqual(self.intern.email, "isha.new@example.com")
        self.assertFalse(SocialAccount.objects.filter(user=self.intern).exists())
        self.assertTrue(EmailAddress.objects.filter(user=self.intern, email="isha.new@example.com", primary=True).exists())

    def test_admin_cannot_delete_self(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("user_delete", args=[self.admin.pk]))
        self.assertRedirects(response, reverse("user_list"))
        self.assertTrue(get_user_model().objects.filter(pk=self.admin.pk).exists())


class GoogleAccessTests(AppTestMixin, TestCase):
    @override_settings(
        GOOGLE_ALLOWED_DOMAINS={"incorrect-app-host.example"},
        REQUIRE_PREPROVISIONED_USERS=True,
        SYSTEM_ADMIN_EMAILS=set(),
        MANAGER_EMAILS=set(),
    )
    def test_exactly_preprovisioned_user_is_not_blocked_by_domain_filter(self):
        self.make_user("intern@flexgcc.com")
        sociallogin = SocialLogin(
            user=get_user_model()(email="intern@flexgcc.com"),
            account=SocialAccount(provider="google", extra_data={"email": "intern@flexgcc.com"}),
        )

        FlexGCCSocialAccountAdapter().pre_social_login(RequestFactory().get("/"), sociallogin)

    @override_settings(
        GOOGLE_ALLOWED_DOMAINS={"incorrect-app-host.example"},
        REQUIRE_PREPROVISIONED_USERS=True,
        SYSTEM_ADMIN_EMAILS={"founder@flexgcc.com"},
        MANAGER_EMAILS=set(),
    )
    def test_bootstrap_admin_is_not_blocked_by_domain_filter(self):
        sociallogin = SocialLogin(
            user=get_user_model()(email="founder@flexgcc.com"),
            account=SocialAccount(provider="google", extra_data={"email": "founder@flexgcc.com"}),
        )

        FlexGCCSocialAccountAdapter().pre_social_login(RequestFactory().get("/"), sociallogin)


class HealthCheckTests(TestCase):
    def test_health_checks_database(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("outreach.views.connection.introspection.table_names", return_value=["django_migrations"])
    def test_health_rejects_database_without_required_tables(self, _table_names):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})
