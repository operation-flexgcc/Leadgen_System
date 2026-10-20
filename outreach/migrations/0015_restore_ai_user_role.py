from django.db import migrations, models


ROLE_CHOICES = [
    ("intern", "Sales intern"),
    ("inside_sales", "Inside sales"),
    ("linkedin_outreach", "LinkedIn outreach"),
    ("account_executive", "Client account executive"),
    ("manager", "Manager"),
    ("system_admin", "System admin"),
    ("ai_user", "AI user"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("outreach", "0014_alter_apiaccesstoken_role_alter_profile_role_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="apiaccesstoken",
            name="role",
            field=models.CharField(choices=ROLE_CHOICES, max_length=20),
        ),
        migrations.AlterField(
            model_name="profile",
            name="role",
            field=models.CharField(
                choices=ROLE_CHOICES, db_index=True, default="intern", max_length=20
            ),
        ),
        migrations.AlterField(
            model_name="profilerole",
            name="role",
            field=models.CharField(choices=ROLE_CHOICES, db_index=True, max_length=20),
        ),
    ]
