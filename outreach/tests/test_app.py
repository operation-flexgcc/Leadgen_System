from datetime import timedelta

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from outreach.forms import ProspectForm
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
        values = {
            "owner": owner,
            "created_by": overrides.pop("created_by", owner),
            "company_name": company,
            "website": "https://example.com",
            "short_description": "Healthcare operations platform.",
            "contact_name": "Asha Rao",
            "contact_email": "asha@example.com",
            "status": Prospect.Status.NOT_RESPONDED,
        }
        values.update(overrides)
        return Prospect.objects.create(**values)


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


class HealthCheckTests(TestCase):
    def test_health_checks_database(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
