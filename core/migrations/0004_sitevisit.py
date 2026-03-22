from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0003_tourvisit"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteVisit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ip_address", models.GenericIPAddressField(unique=True)),
                ("first_seen", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
