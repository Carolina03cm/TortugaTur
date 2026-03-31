from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_sitevisit_cookie_visitors"),
    ]

    operations = [
        migrations.AddField(
            model_name="tour",
            name="ocultar_precio",
            field=models.BooleanField(
                default=False,
                help_text="Si se activa, el tour no mostrara tarifa publica y la reserva se gestionara internamente.",
                verbose_name="Ocultar precio al cliente",
            ),
        ),
    ]
