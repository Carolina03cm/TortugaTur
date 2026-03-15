from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_empresaconfig_reserva_agencia_contacto_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="reserva",
            name="alerta_7d_agencia_enviada_en",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
