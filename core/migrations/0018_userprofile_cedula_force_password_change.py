from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_reserva_alerta_24h_agencia_enviada_en"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="cedula",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="userprofile",
            name="force_password_change",
            field=models.BooleanField(default=False),
        ),
    ]
