from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_empresaconfig_reserva_agencia_contacto_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TourVisit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ip_address", models.GenericIPAddressField()),
                ("first_seen", models.DateTimeField(auto_now_add=True)),
                ("last_seen", models.DateTimeField(auto_now=True)),
                ("tour", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="visitas_ip", to="core.tour")),
            ],
        ),
        migrations.AddConstraint(
            model_name="tourvisit",
            constraint=models.UniqueConstraint(fields=("tour", "ip_address"), name="unique_tour_visit_ip"),
        ),
    ]
