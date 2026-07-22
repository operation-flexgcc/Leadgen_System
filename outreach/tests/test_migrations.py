from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CompanyIdentityMigrationTests(TransactionTestCase):
    migrate_from = ("outreach", "0003_prospect_import_key_prospect_import_source_and_more")
    migrate_to = ("outreach", "0004_company_identity_and_api_tokens")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        old_apps = self.executor.loader.project_state([self.migrate_from]).apps
        User = old_apps.get_model("auth", "User")
        Prospect = old_apps.get_model("outreach", "Prospect")
        creator = User.objects.create(username="migration-admin", email="admin@example.com")

        common = {
            "owner_id": None,
            "stage": "research",
            "company_name": "Shared Advisory",
            "location": "Chicago, IL",
            "created_by_id": creator.pk,
        }
        Prospect.objects.create(
            **common,
            workstream="intern",
            website="https://shared.example.com",
            import_key="shared.example.com",
        )
        Prospect.objects.create(
            **common,
            workstream="linkedin_outreach",
            website="https://www.shared.example.com/about",
            import_key="shared.example.com",
        )
        Prospect.objects.create(
            owner_id=None,
            workstream="inside_sales",
            stage="research",
            company_name="Distinct Advisory",
            website="https://distinct.example.com",
            location="Boston, MA",
            created_by_id=creator.pk,
        )

        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_to])
        self.apps = self.executor.loader.project_state([self.migrate_to]).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_records_are_backfilled_and_cross_workstream_duplicates_share_id(self):
        Prospect = self.apps.get_model("outreach", "Prospect")
        CompanyIdentity = self.apps.get_model("outreach", "CompanyIdentity")
        shared = Prospect.objects.filter(company_name="Shared Advisory")

        self.assertEqual(Prospect.objects.filter(company_id__isnull=True).count(), 0)
        self.assertEqual(CompanyIdentity.objects.count(), 2)
        self.assertEqual(shared.values("company_id").distinct().count(), 1)
