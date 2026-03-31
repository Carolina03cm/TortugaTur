from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_tour_contenido_detallado"),
    ]

    operations = [
        migrations.AlterField(
            model_name="reserva",
            name="estado",
            field=models.CharField(
                choices=[
                    ("pendiente", "Pendiente"),
                    ("cotizacion_pendiente", "Cotizacion Pendiente"),
                    ("solicitud_agencia", "Solicitud Agencia"),
                    ("cotizada_agencia", "Cotizada Agencia"),
                    ("confirmada_agencia", "Confirmada Agencia"),
                    ("pagada_parcial_agencia", "Pagada Parcial Agencia"),
                    ("pagada_total_agencia", "Pagada Total Agencia"),
                    ("rechazada_agencia", "Rechazada Agencia"),
                    ("confirmada", "Confirmada"),
                    ("pagada", "Pagada"),
                    ("cancelada", "Cancelada"),
                    ("bloqueada_por_agencia", "Bloqueada por Agencia"),
                ],
                default="pendiente",
                max_length=30,
            ),
        ),
    ]
