from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("outreach", "0016_lead_intake_approval_and_ownership")]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="password_provisioned_by_admin",
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
