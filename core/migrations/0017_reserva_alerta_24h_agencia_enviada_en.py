from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_reserva_turnos_agencia"),
    ]

    operations = [
        migrations.AddField(
            model_name="reserva",
            name="alerta_24h_agencia_enviada_en",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

