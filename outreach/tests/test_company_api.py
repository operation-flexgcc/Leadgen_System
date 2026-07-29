import csv
import hashlib
import json
import uuid
from datetime import timedelta
from io import BytesIO, StringIO

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from outreach.api_tokens import (
    API_ACCESS_TOKEN_PREFIX,
    ApiTokenError,
    authenticate_access_token,
    issue_token_pair,
)
from outreach.models import (
    ApiAccessToken,
    ApiRefreshToken,
    CompanyUpdateAudit,
    Profile,
    Prospect,
    ProspectUpdateAudit,
)


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

    def test_logged_in_user_generates_persistent_hashed_access_token(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("api_access"))

        self.assertEqual(response.status_code, 200)
        token_data = response.context["token_pair"]
        raw_token = token_data["access_token"]
        self.assertTrue(raw_token.startswith(API_ACCESS_TOKEN_PREFIX))
        self.assertIsNone(token_data["expires_in"])
        self.assertNotIn("refresh_token", token_data)

        stored = ApiAccessToken.objects.get(user=self.user)
        self.assertNotEqual(stored.token_hash, raw_token)
        self.assertEqual(
            stored.token_hash,
            hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(stored.token_prefix, raw_token[:16])
        self.assertContains(response, "No automatic expiry")

    def test_persistent_token_does_not_expire_based_on_age(self):
        raw_token = issue_token_pair(self.user)["access_token"]
        token = ApiAccessToken.objects.get(user=self.user)
        ApiAccessToken.objects.filter(pk=token.pk).update(
            created_at=timezone.now() - timedelta(days=3650)
        )

        authenticated_user = authenticate_access_token(raw_token)

        self.assertEqual(authenticated_user, self.user)
        token.refresh_from_db()
        self.assertIsNotNone(token.last_used_at)

    def test_user_can_revoke_own_token_and_it_stops_working_immediately(self):
        prospect = self.make_prospect(
            self.user,
            "Persistent Token Advisory",
            "https://persistent-token.example.com",
        )
        raw_token = issue_token_pair(self.user)["access_token"]
        token = ApiAccessToken.objects.get(user=self.user)
        company_url = reverse("api_company_detail", args=[prospect.company_id])
        self.assertEqual(
            self.client.get(
                company_url,
                HTTP_AUTHORIZATION=f"Bearer {raw_token}",
            ).status_code,
            200,
        )

        self.client.force_login(self.user)
        revoke_response = self.client.post(
            reverse("api_access_token_revoke", args=[token.pk])
        )

        self.assertRedirects(revoke_response, reverse("api_access"))
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        rejected = self.client.get(
            company_url,
            HTTP_AUTHORIZATION=f"Bearer {raw_token}",
        )
        self.assertEqual(rejected.status_code, 401)
        self.assertIn("revoked", rejected.json()["error"])

    def test_user_cannot_revoke_another_users_token(self):
        other_user = self.make_user("other@example.com", name="Omar Intern")
        issue_token_pair(other_user)
        other_token = ApiAccessToken.objects.get(user=other_user)
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("api_access_token_revoke", args=[other_token.pk])
        )

        self.assertEqual(response.status_code, 404)
        other_token.refresh_from_db()
        self.assertIsNone(other_token.revoked_at)

    def test_token_is_rejected_when_its_user_is_inactive(self):
        raw_token = issue_token_pair(self.user)["access_token"]
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        with self.assertRaisesMessage(ApiTokenError, "unavailable or inactive"):
            authenticate_access_token(raw_token)

    def test_old_refresh_token_is_exchanged_once_for_persistent_token(self):
        raw_refresh_token = "old-refresh-token-value"
        ApiRefreshToken.objects.create(
            user=self.user,
            token_hash=hashlib.sha256(raw_refresh_token.encode("utf-8")).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        response = self.client.post(
            reverse("api_token_refresh"),
            data=json.dumps({"refresh_token": raw_refresh_token}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["access_token"].startswith(API_ACCESS_TOKEN_PREFIX))
        self.assertIsNone(response.json()["expires_in"])
        self.assertNotIn("refresh_token", response.json())
        self.assertEqual(ApiAccessToken.objects.filter(user=self.user).count(), 1)

        reused = self.client.post(
            reverse("api_token_refresh"),
            data=json.dumps({"refresh_token": raw_refresh_token}),
            content_type="application/json",
        )
        self.assertEqual(reused.status_code, 401)

    def test_unexpired_legacy_jwt_remains_compatible(self):
        now = timezone.now()
        legacy_token = jwt.encode(
            {
                "iss": settings.API_TOKEN_ISSUER,
                "aud": settings.API_TOKEN_AUDIENCE,
                "sub": str(self.user.pk),
                "email": self.user.email,
                "type": "access",
                "jti": uuid.uuid4().hex,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=15)).timestamp()),
            },
            settings.API_TOKEN_SIGNING_KEY,
            algorithm="HS256",
        )

        self.assertEqual(authenticate_access_token(legacy_token), self.user)


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

    def test_claim_row_lock_does_not_join_nullable_owner(self):
        unassigned = Prospect.objects.create(
            owner=None,
            workstream=Prospect.Workstream.INTERN,
            stage=Prospect.Stage.RESEARCH,
            company_name="ORENG Consulting",
            website="http://www.orengconsulting.com",
            location="Boston, MA",
            created_by=self.manager,
        )

        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(
                reverse("api_company_claim", args=[unassigned.company_id]),
                HTTP_AUTHORIZATION=self.bearer(self.other_intern),
            )

        self.assertEqual(response.status_code, 200, response.content)
        claim_selects = [
            query["sql"]
            for query in queries.captured_queries
            if 'FROM "outreach_prospect"' in query["sql"]
            and '"outreach_prospect"."company_id"' in query["sql"]
            and query["sql"].lstrip().upper().startswith("SELECT")
        ]
        self.assertTrue(claim_selects)
        for claim_select in claim_selects:
            self.assertNotIn('JOIN "auth_user"', claim_select)


class ProspectWorkflowApiTests(CompanyApiTestMixin, TestCase):
    def setUp(self):
        self.linkedin_user = self.make_user(
            "linkedin@example.com",
            Profile.Role.LINKEDIN_OUTREACH,
            "Leena LinkedIn",
        )
        self.other_linkedin_user = self.make_user(
            "other-linkedin@example.com",
            Profile.Role.LINKEDIN_OUTREACH,
            "Omar LinkedIn",
        )
        self.intern = self.make_user("intern@example.com", name="Isha Intern")
        self.manager = self.make_user(
            "manager@example.com",
            Profile.Role.MANAGER,
            "Meera Manager",
        )
        self.prospect = self.make_prospect(
            self.linkedin_user,
            "LinkedIn Advisory",
            "https://linkedin-advisory.example.com",
            contact_linkedin_url="https://www.linkedin.com/in/asha-rao",
            founder_account=Prospect.FounderAccount.KANDARP_SONI,
            linkedin_connection_status=Prospect.LinkedInConnectionStatus.NOT_SENT,
            personalization_note="Chicago operations practice.",
        )
        self.url = reverse("api_prospect_detail", args=[self.prospect.pk])

    def patch_prospect(self, user, payload, *, prospect=None):
        prospect = prospect or self.prospect
        return self.client.generic(
            "PATCH",
            reverse("api_prospect_detail", args=[prospect.pk]),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.bearer(user),
        )

    def test_manager_updates_complete_founder_section_and_new_flags_with_audit(self):
        response = self.patch_prospect(
            self.manager,
            {
                "founder_account": Prospect.FounderAccount.SUNIT_KALA,
                "linkedin_connection_status": Prospect.LinkedInConnectionStatus.REQUEST_SENT,
                "personalization_note": "Florida healthcare advisory practice.",
                "founder_escalation_required": True,
                "founder_escalation_notes": "Founder should answer the pricing question.",
                "prospect_sent": True,
                "is_not_eligible": True,
            },
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.prospect.refresh_from_db()
        self.assertEqual(self.prospect.founder_account, Prospect.FounderAccount.SUNIT_KALA)
        self.assertEqual(
            self.prospect.linkedin_connection_status,
            Prospect.LinkedInConnectionStatus.REQUEST_SENT,
        )
        self.assertTrue(self.prospect.founder_escalation_required)
        self.assertTrue(self.prospect.prospect_sent)
        self.assertTrue(self.prospect.is_not_eligible)
        self.assertEqual(response.json()["modified_by"]["name"], "Meera Manager")
        audit = ProspectUpdateAudit.objects.get(prospect=self.prospect)
        self.assertEqual(audit.modified_by, self.manager)
        self.assertEqual(
            audit.previous_values["founder_account"],
            Prospect.FounderAccount.KANDARP_SONI,
        )
        self.assertTrue(audit.changed_values["prospect_sent"])

    def test_owner_can_update_and_another_frontline_user_cannot(self):
        updated = self.patch_prospect(
            self.linkedin_user,
            {"prospect_sent": True, "is_not_eligible": False},
        )
        rejected = self.patch_prospect(
            self.other_linkedin_user,
            {"prospect_sent": False},
        )

        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(rejected.status_code, 403)
        self.prospect.refresh_from_db()
        self.assertTrue(self.prospect.prospect_sent)

    def test_non_linkedin_prospect_accepts_flags_but_rejects_founder_fields(self):
        intern_prospect = self.make_prospect(
            self.intern,
            "Intern Advisory",
            "https://intern-advisory.example.com",
        )
        flags_response = self.patch_prospect(
            self.intern,
            {"prospect_sent": True, "is_not_eligible": True},
            prospect=intern_prospect,
        )
        founder_response = self.patch_prospect(
            self.intern,
            {"founder_account": Prospect.FounderAccount.KANDARP_SONI},
            prospect=intern_prospect,
        )

        self.assertEqual(flags_response.status_code, 200, flags_response.content)
        self.assertEqual(founder_response.status_code, 400)
        self.assertIn(
            "founder_account",
            founder_response.json()["field_errors"],
        )
        intern_prospect.refresh_from_db()
        self.assertTrue(intern_prospect.prospect_sent)
        self.assertTrue(intern_prospect.is_not_eligible)
        self.assertEqual(intern_prospect.founder_account, "")

    def test_invalid_choice_and_string_boolean_return_field_errors(self):
        response = self.patch_prospect(
            self.manager,
            {
                "founder_account": "unknown_founder",
                "prospect_sent": "true",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("founder_account", response.json()["field_errors"])
        self.assertIn("prospect_sent", response.json()["field_errors"])
        self.assertEqual(ProspectUpdateAudit.objects.count(), 0)

    def test_get_returns_workflow_fields_and_company_lists_prospect_ids(self):
        prospect_response = self.client.get(
            self.url,
            HTTP_AUTHORIZATION=self.bearer(self.manager),
        )
        company_response = self.client.get(
            reverse("api_company_detail", args=[self.prospect.company_id]),
            HTTP_AUTHORIZATION=self.bearer(self.manager),
        )

        self.assertEqual(prospect_response.status_code, 200)
        self.assertEqual(
            prospect_response.json()["prospect"]["founder_account"],
            Prospect.FounderAccount.KANDARP_SONI,
        )
        self.assertFalse(prospect_response.json()["prospect"]["prospect_sent"])
        self.assertFalse(prospect_response.json()["prospect"]["is_not_eligible"])
        self.assertIn(
            {
                "id": self.prospect.pk,
                "workstream": Prospect.Workstream.LINKEDIN_OUTREACH,
            },
            company_response.json()["company"]["prospects"],
        )
