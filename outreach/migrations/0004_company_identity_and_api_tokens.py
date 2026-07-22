import uuid
from urllib.parse import urlsplit

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_company_identities(apps, schema_editor):
    CompanyIdentity = apps.get_model("outreach", "CompanyIdentity")
    Prospect = apps.get_model("outreach", "Prospect")
    identities = {}

    for prospect in Prospect.objects.order_by("pk").iterator():
        key = (prospect.import_key or "").strip().lower().removeprefix("www.")
        if not key:
            key = (
                (urlsplit(prospect.website or "").hostname or "")
                .lower()
                .removeprefix("www.")
            )
        if not key:
            key = f"legacy-prospect-{prospect.pk}"

        identity_id = identities.get(key)
        if identity_id is None:
            identity_id = uuid.uuid4()
            CompanyIdentity.objects.create(id=identity_id, key=key)
            identities[key] = identity_id
        Prospect.objects.filter(pk=prospect.pk).update(company_id=identity_id)


class Migration(migrations.Migration):

    dependencies = [
        ("outreach", "0003_prospect_import_key_prospect_import_source_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanyIdentity",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("key", models.CharField(editable=False, max_length=300, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name_plural": "company identities",
                "ordering": ["key"],
            },
        ),
        migrations.AddField(
            model_name="prospect",
            name="company",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                help_text="System-generated immutable company identity shared across workstreams.",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="prospects",
                to="outreach.companyidentity",
            ),
        ),
        migrations.RunPython(backfill_company_identities, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="prospect",
            name="company",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                help_text="System-generated immutable company identity shared across workstreams.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="prospects",
                to="outreach.companyidentity",
            ),
        ),
        migrations.CreateModel(
            name="ApiRefreshToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="api_refresh_tokens",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="CompanyUpdateAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source", models.CharField(choices=[("api", "API")], default="api", max_length=20)),
                ("previous_values", models.JSONField(default=dict)),
                ("changed_values", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "company",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="update_audits",
                        to="outreach.companyidentity",
                    ),
                ),
                (
                    "modified_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="company_update_audits",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
    ]
