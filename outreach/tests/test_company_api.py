import csv
import json
import uuid
from io import BytesIO, StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from openpyxl import load_workbook

from outreach.api_tokens import issue_token_pair
from outreach.models import ApiRefreshToken, CompanyUpdateAudit, Profile, Prospect


class CompanyApiTestMixin:
    def make_user(self, email, role=Profile.Role.INTERN, name="Test User"):
        first_name, _, last_name = name.partition(" ")
        user = get_user_model().objects.create_user(
            username=email,
            email=email,
            first_name=first_name,
            last_name=last_name,
        )
        user.profile.role = role
        user.profile.save(update_fields=["role"])
        return user

    def make_prospect(self, owner, company_name, website, **overrides):
        values = {
            "owner": owner,
            "workstream": owner.profile.role,
            "stage": Prospect.Stage.ELIGIBLE,
            "company_name": company_name,
            "website": website,
            "short_description": "Boutique advisory firm.",
            "location": "Chicago, IL",
            "consulting_focus": "Operations consulting",
            "client_segment": "Mid-market companies",
            "eligibility_evidence": "Published mid-market client work.",
            "contact_name": "Asha Rao",
            "contact_title": "Managing Partner",
            "contact_email": "asha@example.com",
            "created_by": overrides.pop("created_by", owner),
        }
        values.update(overrides)
        return Prospect.objects.create(**values)

    def bearer(self, user):
        return f"Bearer {issue_token_pair(user)['access_token']}"


class CompanyIdentityTests(CompanyApiTestMixin, TestCase):
    def test_same_domain_shares_one_system_generated_company_id_across_workstreams(self):
        intern = self.make_user("intern@example.com")
        inside_sales = self.make_user("inside@example.com", Profile.Role.INSIDE_SALES)
        first = self.make_prospect(intern, "Shared Advisory", "https://shared.example.com")
        second = self.make_prospect(
            inside_sales,
            "Shared Advisory",
            "https://www.shared.example.com/about",
        )

        self.assertEqual(first.company_id, second.company_id)
        self.assertIsInstance(first.company_id, uuid.UUID)

    def test_company_id_cannot_be_changed_after_save(self):
        intern = self.make_user("intern@example.com")
        prospect = self.make_prospect(intern, "Stable ID Advisory", "https://stable.example.com")
        prospect.company_id = uuid.uuid4()

        with self.assertRaises(ValidationError) as error:
            prospect.save()

        self.assertIn("cannot be changed", error.exception.message_dict["company"][0])


class CompanyExportTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user("intern@example.com", name="Isha Intern")
        self.inside = self.make_user("inside@example.com", Profile.Role.INSIDE_SALES, "Ivan Inside")
        self.manager = self.make_user("manager@example.com", Profile.Role.MANAGER, "Meera Manager")
        self.first = self.make_prospect(
            self.intern,
            "Shared Advisory",
            "https://shared.example.com",
            location="Miami, FL",
        )
        self.make_prospect(
            self.inside,
            "Shared Advisory",
            "https://www.shared.example.com/about",
            location="Miami, FL",
        )

    def test_csv_export_has_exact_columns_and_deduplicates_company_ids(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("company_export", args=["csv"]))

        self.assertEqual(response.status_code, 200)
        rows = list(csv.reader(StringIO(response.content.decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["ID", "Name", "Location", "URL"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], str(self.first.company_id))
        self.assertEqual(rows[1][1:], ["Shared Advisory", "Miami, FL", "https://shared.example.com"])

    def test_xlsx_export_is_scoped_and_formatted_for_the_logged_in_user(self):
        self.client.force_login(self.intern)
        response = self.client.get(reverse("company_export", args=["xlsx"]))

        self.assertEqual(response.status_code, 200)
        workbook = load_workbook(BytesIO(response.content), read_only=False)
        worksheet = workbook["Companies"]
        self.assertEqual([cell.value for cell in worksheet[1]], ["ID", "Name", "Location", "URL"])
        self.assertEqual(worksheet.max_row, 2)
        self.assertEqual(worksheet["A2"].value, str(self.first.company_id))
        self.assertEqual(worksheet.freeze_panes, "A2")
        self.assertEqual(worksheet.auto_filter.ref, "A1:D2")


class TokenLifecycleTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user("intern@example.com", name="Isha Intern")

    def test_logged_in_user_can_generate_access_and_refresh_tokens(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("api_access"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["token_pair"]["access_token"])
        self.assertTrue(response.context["token_pair"]["refresh_token"])
        stored = ApiRefreshToken.objects.get(user=self.user)
        self.assertNotEqual(stored.token_hash, response.context["token_pair"]["refresh_token"])

    def test_refresh_token_rotates_once_and_cannot_be_reused(self):
        pair = issue_token_pair(self.user)
        response = self.client.post(
            reverse("api_token_refresh"),
            data=json.dumps({"refresh_token": pair["refresh_token"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["refresh_token"], pair["refresh_token"])

        reused = self.client.post(
            reverse("api_token_refresh"),
            data=json.dumps({"refresh_token": pair["refresh_token"]}),
            content_type="application/json",
        )
        self.assertEqual(reused.status_code, 401)


class CompanyApiTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user("intern@example.com", name="Isha Intern")
        self.other_intern = self.make_user("other@example.com", name="Omar Intern")
        self.inside = self.make_user("inside@example.com", Profile.Role.INSIDE_SALES, "Ivan Inside")
        self.manager = self.make_user("manager@example.com", Profile.Role.MANAGER, "Meera Manager")
        self.first = self.make_prospect(
            self.intern,
            "Shared Advisory",
            "https://shared.example.com",
        )
        self.second = self.make_prospect(
            self.inside,
            "Shared Advisory",
            "https://www.shared.example.com/about",
        )

    def patch_company(self, user, payload):
        return self.client.generic(
            "PATCH",
            reverse("api_company_detail", args=[self.first.company_id]),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.bearer(user),
        )

    def test_manager_update_propagates_to_all_workstreams_and_is_audited(self):
        response = self.patch_company(
            self.manager,
            {
                "short_description": "Updated canonical description.",
                "eligibility_evidence": "Verified boutique consultancy.",
                "linkedin_url": "https://www.linkedin.com/in/asha-rao",
                "email": "new@example.com",
                "phone": "+1 312 555 0100",
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        for prospect in (self.first, self.second):
            self.assertEqual(prospect.short_description, "Updated canonical description.")
            self.assertEqual(prospect.contact_email, "new@example.com")
            self.assertEqual(prospect.contact_phone, "+1 312 555 0100")
        audit = CompanyUpdateAudit.objects.get(company_id=self.first.company_id)
        self.assertEqual(audit.modified_by, self.manager)
        self.assertEqual(audit.changed_values["email"], "new@example.com")
        self.assertEqual(response.json()["modified_by"]["name"], "Meera Manager")

    def test_company_id_and_unsupported_fields_cannot_be_modified_by_api(self):
        original_id = self.first.company_id
        response = self.patch_company(self.manager, {"id": str(uuid.uuid4())})

        self.assertEqual(response.status_code, 400)
        self.first.refresh_from_db()
        self.assertEqual(self.first.company_id, original_id)
        self.assertEqual(CompanyUpdateAudit.objects.count(), 0)

    def test_access_token_is_required(self):
        response = self.client.generic(
            "PATCH",
            reverse("api_company_detail", args=[self.first.company_id]),
            data=json.dumps({"contact_name": "Updated"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_frontline_user_must_claim_before_api_update(self):
        unassigned = Prospect.objects.create(
            owner=None,
            workstream=Prospect.Workstream.INTERN,
            stage=Prospect.Stage.RESEARCH,
            company_name="Unassigned Advisory",
            website="https://unassigned.example.com",
            created_by=self.manager,
        )
        url = reverse("api_company_detail", args=[unassigned.company_id])
        before_claim = self.client.generic(
            "PATCH",
            url,
            data=json.dumps({"contact_name": "New Contact"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.bearer(self.other_intern),
        )
        self.assertEqual(before_claim.status_code, 403)

        claim = self.client.post(
            reverse("api_company_claim", args=[unassigned.company_id]),
            HTTP_AUTHORIZATION=self.bearer(self.other_intern),
        )
        self.assertEqual(claim.status_code, 200)
        updated = self.client.generic(
            "PATCH",
            url,
            data=json.dumps({"contact_name": "New Contact"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.bearer(self.other_intern),
        )
        self.assertEqual(updated.status_code, 200, updated.content)

    def test_claim_conflict_returns_existing_users_name(self):
        claimed = Prospect.objects.create(
            owner=self.intern,
            workstream=Prospect.Workstream.INTERN,
            stage=Prospect.Stage.RESEARCH,
            company_name="Claimed Advisory",
            website="https://claimed.example.com",
            created_by=self.manager,
        )
        response = self.client.post(
            reverse("api_company_claim", args=[claimed.company_id]),
            HTTP_AUTHORIZATION=self.bearer(self.other_intern),
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["claimed_by"], "Isha Intern")
        self.assertIn("already claimed by Isha Intern", response.json()["error"])
