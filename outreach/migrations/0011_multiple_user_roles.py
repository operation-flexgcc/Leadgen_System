import django.db.models.deletion
from django.db import migrations, models


ROLE_CHOICES = [
    ("intern", "Sales intern"),
    ("inside_sales", "Inside sales"),
    ("linkedin_outreach", "LinkedIn outreach"),
    ("account_executive", "Client account executive"),
    ("manager", "Manager"),
    ("system_admin", "System admin"),
]


def preserve_existing_token_roles(apps, schema_editor):
    profiles = apps.get_model("outreach", "Profile")
    tokens = apps.get_model("outreach", "ApiAccessToken")
    database = schema_editor.connection.alias
    for role, _ in ROLE_CHOICES:
        users = profiles.objects.using(database).filter(role=role).values("user_id")
        tokens.objects.using(database).filter(user_id__in=users).update(role=role)


class Migration(migrations.Migration):
    dependencies = [
        ("outreach", "0010_alter_profile_role_alter_prospect_website_escalation_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProfileRole",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=ROLE_CHOICES, db_index=True, max_length=20)),
                ("profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="additional_roles", to="outreach.profile")),
            ],
            options={
                "constraints": [models.UniqueConstraint(fields=("profile", "role"), name="unique_additional_profile_role")],
            },
        ),
        migrations.AddField(
            model_name="apiaccesstoken",
            name="role",
            field=models.CharField(choices=ROLE_CHOICES, max_length=20, default=""),
            preserve_default=False,
        ),
        migrations.RunPython(preserve_existing_token_roles, migrations.RunPython.noop),
    ]
