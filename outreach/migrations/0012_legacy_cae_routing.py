import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("outreach", "0011_multiple_user_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="escalation",
            name="escalated_by",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="submitted_escalations", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="FounderCAEAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("founder_account", models.CharField(max_length=30, unique=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("executive", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="founder_cae_assignments", to=settings.AUTH_USER_MODEL)),
                ("updated_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="updated_cae_assignments", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
