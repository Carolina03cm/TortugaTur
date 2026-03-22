from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_sitevisit"),
    ]

    operations = [
        migrations.DeleteModel(
            name="TourVisit",
        ),
    ]
