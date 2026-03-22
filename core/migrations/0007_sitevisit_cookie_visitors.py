from django.db import migrations, models


def populate_sitevisit_keys(apps, schema_editor):
    SiteVisit = apps.get_model("core", "SiteVisit")
    for visit in SiteVisit.objects.all().order_by("id"):
        if not getattr(visit, "visitor_key", ""):
            base = (visit.ip_address or "").replace(":", "-").replace(".", "-") or "no-ip"
            visit.visitor_key = f"ip-{base}-{visit.id}"
            visit.save(update_fields=["visitor_key"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_normalize_sitevisit_ips"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitevisit",
            name="visitor_key",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.RunPython(populate_sitevisit_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="sitevisit",
            name="visitor_key",
            field=models.CharField(max_length=64, unique=True),
        ),
        migrations.AlterField(
            model_name="sitevisit",
            name="ip_address",
            field=models.GenericIPAddressField(blank=True, null=True),
        ),
    ]
