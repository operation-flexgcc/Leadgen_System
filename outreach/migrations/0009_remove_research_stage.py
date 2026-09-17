from django.db import migrations, models


def promote_research_prospects(apps, schema_editor):
    Prospect = apps.get_model("outreach", "Prospect")
    Prospect.objects.filter(stage="research").update(stage="eligible")


class Migration(migrations.Migration):
    dependencies = [
        ("outreach", "0008_outreach_update_audit"),
    ]

    operations = [
        migrations.RunPython(promote_research_prospects, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="prospect",
            name="stage",
            field=models.CharField(
                choices=[
                    ("eligible", "Eligible"),
                    ("contacted", "Contacted"),
                    ("responded", "Responded"),
                    ("interested", "Interested"),
                    ("founder_meeting_booked", "Founder meeting booked"),
                    ("meeting_completed", "Meeting completed"),
                    ("closed", "Closed"),
                ],
                db_index=True,
                default="eligible",
                max_length=40,
            ),
        ),
    ]
