import csv
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from outreach.models import Outreach, Profile, Prospect, ProspectImportBatch


class BulkImportTestMixin:
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

    def make_prospect(self, owner, company_name, website, **overrides):
        values = {
            "owner": owner,
            "created_by": overrides.pop("created_by", owner),
            "workstream": overrides.pop("workstream", owner.profile.role),
            "stage": Prospect.Stage.ELIGIBLE,
            "company_name": company_name,
            "website": website,
            "short_description": "Boutique consulting firm.",
            "location": "Austin, TX",
            "consulting_focus": "Operations consulting",
            "client_segment": "Mid-market businesses",
            "eligibility_evidence": "Published mid-market client work.",
            "contact_name": "Asha Rao",
            "contact_title": "Managing Partner",
            "contact_email": "asha@example.com",
        }
        values.update(overrides)
        return Prospect.objects.create(**values)

    def csv_upload(self, rows, *, headers=None, name="prospects.csv"):
        output = StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers or ["Company Name", "Website", "Area"])
        writer.writerows(rows)
        return SimpleUploadedFile(
            name,
            output.getvalue().encode("utf-8"),
            content_type="text/csv",
        )

    def upload(self, user, rows, **form_overrides):
        self.client.force_login(user)
        payload = {
            "workstream": (
                user.profile.role
                if user.profile.role in Prospect.Workstream.values
                else Prospect.Workstream.INTERN
            ),
            "csv_file": self.csv_upload(rows),
        }
        payload.update(form_overrides)
        return self.client.post(reverse("prospect_bulk_import"), payload)


class BulkProspectImportTests(BulkImportTestMixin, TestCase):
    def setUp(self):
        self.intern = self.make_user(
            "intern-import@example.com",
            Profile.Role.INTERN,
            "Isha Intern",
        )
        self.other_intern = self.make_user(
            "other-import@example.com",
            Profile.Role.INTERN,
            "Omar Intern",
        )
        self.inside_sales = self.make_user(
            "inside-import@example.com",
            Profile.Role.INSIDE_SALES,
            "Ivan Inside",
        )
        self.manager = self.make_user(
            "manager-import@example.com",
            Profile.Role.MANAGER,
            "Meera Manager",
        )

    def test_bulk_import_is_discoverable_and_requires_login(self):
        protected = self.client.get(reverse("prospect_bulk_import"))
        self.client.force_login(self.intern)
        add_page = self.client.get(reverse("prospect_create"))
        dashboard = self.client.get(reverse("dashboard"))

        self.assertEqual(protected.status_code, 302)
        self.assertContains(add_page, reverse("prospect_bulk_import"))
        self.assertContains(add_page, "Bulk ingest a CSV")
        self.assertContains(dashboard, "Import CSV")

    def test_frontline_import_adds_valid_rows_and_lists_every_ignored_row(self):
        existing = self.make_prospect(
            self.intern,
            "Existing Advisory",
            "https://existing-advisory.example.com",
        )
        response = self.upload(
            self.intern,
            [
                ["Valid Advisory", "https://valid-advisory.example.com", "Austin, TX"],
                ["Missing Area", "https://missing-area.example.com", ""],
                ["Domain Duplicate", "https://www.valid-advisory.example.com/team", "Dallas, TX"],
                ["Existing Duplicate", existing.website, "Houston, TX"],
                ["Invalid URL", "invalid-url", "Atlanta, GA"],
                ["Second Valid", "https://second-valid.example.com", "Atlanta, GA"],
                ["Second Valid", "https://different-domain.example.com", "Savannah, GA"],
            ],
        )

        batch = ProspectImportBatch.objects.get()
        self.assertRedirects(
            response,
            reverse("prospect_bulk_import_result", args=[batch.pk]),
        )
        self.assertEqual(batch.total_rows, 7)
        self.assertEqual(batch.created_count, 2)
        self.assertEqual(batch.skipped_count, 5)
        self.assertEqual(
            [row["row_number"] for row in batch.skipped_rows],
            [3, 4, 5, 6, 8],
        )
        imported = Prospect.objects.filter(import_batch=batch).order_by("company_name")
        self.assertEqual(imported.count(), 2)
        self.assertTrue(all(item.owner == self.intern for item in imported))
        self.assertTrue(all(item.workstream == Prospect.Workstream.INTERN for item in imported))
        self.assertTrue(all(item.stage == Prospect.Stage.RESEARCH for item in imported))
        self.assertEqual(existing.import_batch_id, None)

        result = self.client.get(
            reverse("prospect_bulk_import_result", args=[batch.pk])
        )
        self.assertContains(result, "Valid Advisory")
        self.assertContains(result, "Second Valid")
        self.assertContains(result, "Missing required value(s): Area")
        self.assertContains(result, "Duplicate Website in this CSV")
        self.assertContains(result, "Duplicate Website in the Sales intern workstream")
        self.assertContains(result, "Website must be a complete http:// or https:// URL")
        self.assertContains(result, "Duplicate Company Name in this CSV")
        self.assertContains(result, "Roll back this batch")
        history = self.client.get(reverse("prospect_bulk_import"))
        self.assertContains(history, "Recent CSV ingestions")
        self.assertContains(history, "prospects.csv")
        self.assertContains(
            history,
            reverse("prospect_bulk_import_result", args=[batch.pk]),
        )

    def test_wrong_headers_are_rejected_without_creating_a_batch(self):
        self.client.force_login(self.intern)
        response = self.client.post(
            reverse("prospect_bulk_import"),
            {
                "workstream": Prospect.Workstream.INTERN,
                "csv_file": self.csv_upload(
                    [["Acme", "https://acme.example.com", "Austin, TX"]],
                    headers=["Company", "URL", "Location"],
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            "Use exactly these three columns in this order",
        )
        self.assertEqual(ProspectImportBatch.objects.count(), 0)
        self.assertEqual(Prospect.objects.count(), 0)

    def test_manager_can_choose_matching_owner_and_workstream(self):
        response = self.upload(
            self.manager,
            [["Inside Advisory", "https://inside-advisory.example.com", "Dallas, TX"]],
            workstream=Prospect.Workstream.INSIDE_SALES,
            owner=self.inside_sales.pk,
        )

        batch = ProspectImportBatch.objects.get()
        prospect = Prospect.objects.get(import_batch=batch)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(batch.owner, self.inside_sales)
        self.assertEqual(batch.target_workstream, Prospect.Workstream.INSIDE_SALES)
        self.assertEqual(prospect.owner, self.inside_sales)
        self.assertEqual(prospect.workstream, Prospect.Workstream.INSIDE_SALES)
        self.assertEqual(prospect.created_by, self.manager)

    def test_manager_cannot_assign_owner_from_another_workstream(self):
        response = self.upload(
            self.manager,
            [["Mismatch Advisory", "https://mismatch.example.com", "Dallas, TX"]],
            workstream=Prospect.Workstream.INSIDE_SALES,
            owner=self.intern.pk,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "class matches the target workstream")
        self.assertEqual(ProspectImportBatch.objects.count(), 0)

    def test_same_company_in_another_workstream_is_not_a_duplicate(self):
        self.make_prospect(
            self.inside_sales,
            "Shared Company",
            "https://shared-company.example.com",
        )
        response = self.upload(
            self.intern,
            [["Shared Company", "https://shared-company.example.com", "Austin, TX"]],
        )

        self.assertEqual(response.status_code, 302)
        batch = ProspectImportBatch.objects.get()
        self.assertEqual(batch.created_count, 1)
        self.assertEqual(batch.skipped_count, 0)
        self.assertEqual(
            Prospect.objects.filter(company_id=Prospect.objects.get(import_batch=batch).company_id).count(),
            2,
        )

    def test_rollback_removes_only_prospects_from_that_batch_and_is_idempotent(self):
        existing = self.make_prospect(
            self.intern,
            "Existing Before Batch",
            "https://existing-before.example.com",
        )
        self.upload(
            self.intern,
            [
                ["Rollback One", "https://rollback-one.example.com", "Austin, TX"],
                ["Rollback Two", "https://rollback-two.example.com", "Dallas, TX"],
            ],
        )
        batch = ProspectImportBatch.objects.get()

        first = self.client.post(
            reverse("prospect_bulk_import_rollback", args=[batch.pk])
        )
        batch.refresh_from_db()
        second = self.client.post(
            reverse("prospect_bulk_import_rollback", args=[batch.pk])
        )

        self.assertRedirects(
            first,
            reverse("prospect_bulk_import_result", args=[batch.pk]),
        )
        self.assertRedirects(
            second,
            reverse("prospect_bulk_import_result", args=[batch.pk]),
        )
        self.assertEqual(batch.status, ProspectImportBatch.Status.ROLLED_BACK)
        self.assertEqual(batch.rolled_back_count, 2)
        self.assertEqual(batch.prospects.count(), 0)
        self.assertTrue(Prospect.objects.filter(pk=existing.pk).exists())
        result = self.client.get(
            reverse("prospect_bulk_import_result", args=[batch.pk])
        )
        self.assertContains(result, "fully rolled back")
        self.assertContains(result, "Removed", count=2)

    def test_rollback_protects_modified_records_and_removes_unchanged_records(self):
        self.upload(
            self.intern,
            [
                ["Protected Prospect", "https://protected.example.com", "Austin, TX"],
                ["Active Prospect", "https://active.example.com", "Houston, TX"],
                ["Unchanged Prospect", "https://unchanged.example.com", "Dallas, TX"],
            ],
        )
        batch = ProspectImportBatch.objects.get()
        protected = Prospect.objects.get(
            import_batch=batch,
            company_name="Protected Prospect",
        )
        protected.comments = "Follow-up research was added after import."
        protected.save(update_fields=["comments", "updated_at"])
        active = Prospect.objects.get(
            import_batch=batch,
            company_name="Active Prospect",
        )
        Outreach.objects.create(
            prospect=active,
            sequence_number=1,
            activity_type=Outreach.ActivityType.INITIAL_OUTREACH,
            medium=Outreach.Medium.EMAIL,
            outreach_date=timezone.localdate(),
            recorded_by=self.intern,
        )

        response = self.client.post(
            reverse("prospect_bulk_import_rollback", args=[batch.pk])
        )
        batch.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            batch.status,
            ProspectImportBatch.Status.PARTIALLY_ROLLED_BACK,
        )
        self.assertEqual(batch.rolled_back_count, 1)
        self.assertEqual(batch.prospects.count(), 2)
        self.assertTrue(Prospect.objects.filter(pk=protected.pk).exists())
        self.assertTrue(Prospect.objects.filter(pk=active.pk).exists())
        self.assertEqual(len(batch.rollback_skipped_rows), 2)
        self.assertIn("modified or now has outreach", batch.rollback_skipped_rows[0]["reason"])

    def test_batch_result_and_rollback_are_private_to_uploader_or_manager(self):
        self.upload(
            self.intern,
            [["Private Batch", "https://private-batch.example.com", "Austin, TX"]],
        )
        batch = ProspectImportBatch.objects.get()
        self.client.force_login(self.other_intern)

        hidden_result = self.client.get(
            reverse("prospect_bulk_import_result", args=[batch.pk])
        )
        hidden_rollback = self.client.post(
            reverse("prospect_bulk_import_rollback", args=[batch.pk])
        )
        hidden_history = self.client.get(reverse("prospect_bulk_import"))
        self.client.force_login(self.manager)
        manager_result = self.client.get(
            reverse("prospect_bulk_import_result", args=[batch.pk])
        )
        manager_history = self.client.get(reverse("prospect_bulk_import"))

        self.assertEqual(hidden_result.status_code, 404)
        self.assertEqual(hidden_rollback.status_code, 404)
        self.assertNotContains(
            hidden_history,
            reverse("prospect_bulk_import_result", args=[batch.pk]),
        )
        self.assertEqual(manager_result.status_code, 200)
        self.assertContains(manager_history, "prospects.csv")
        self.assertEqual(batch.prospects.count(), 1)

    def test_csv_extension_is_required(self):
        self.client.force_login(self.intern)
        response = self.client.post(
            reverse("prospect_bulk_import"),
            {
                "workstream": Prospect.Workstream.INTERN,
                "csv_file": self.csv_upload(
                    [["Acme", "https://acme.example.com", "Austin, TX"]],
                    name="prospects.txt",
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload a file with a .csv extension")
        self.assertEqual(ProspectImportBatch.objects.count(), 0)
