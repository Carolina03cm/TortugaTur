from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_tour_ocultar_precio"),
    ]

    operations = [
        migrations.AddField(
            model_name="tour",
            name="descripcion_experiencia",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Texto completo del recorrido. Puedes pegar varios parrafos.",
                verbose_name="Descripcion de la experiencia",
            ),
        ),
        migrations.AddField(
            model_name="tour",
            name="idiomas",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Ej: Espanol / Ingles",
                max_length=120,
                verbose_name="Idiomas",
            ),
        ),
        migrations.AddField(
            model_name="tour",
            name="incluye",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Escribe un item por linea.",
                verbose_name="Incluye",
            ),
        ),
        migrations.AddField(
            model_name="tour",
            name="informacion_importante",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Escribe un item por linea.",
                verbose_name="Informacion importante",
            ),
        ),
        migrations.AddField(
            model_name="tour",
            name="nivel_dificultad",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Ej: Facil, Media, Alta",
                max_length=50,
                verbose_name="Nivel de dificultad",
            ),
        ),
        migrations.AddField(
            model_name="tour",
            name="no_incluye",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Escribe un item por linea.",
                verbose_name="No incluye",
            ),
        ),
        migrations.AddField(
            model_name="tour",
            name="nota_importante",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Observaciones finales o aclaraciones del tour.",
                verbose_name="Nota importante",
            ),
        ),
        migrations.AddField(
            model_name="tour",
            name="recomendaciones",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Escribe un item por linea.",
                verbose_name="Recomendaciones",
            ),
        ),
        migrations.AlterField(
            model_name="tour",
            name="descripcion",
            field=models.TextField(
                help_text="Resumen breve para tarjetas, listados y encabezado del tour.",
                verbose_name="Descripcion corta",
            ),
        ),
    ]
