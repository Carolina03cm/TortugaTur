from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Destino, Reserva, SalidaTour, Tour


class CheckoutSecurityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pass12345")
        self.other = User.objects.create_user(username="other", password="pass12345")

        destino = Destino.objects.create(
            nombre="Santa Cruz",
            imagen_url="https://example.com/destino.jpg",
        )
        tour = Tour.objects.create(
            nombre="Tour Bahia",
            destino=destino,
            descripcion="Tour de prueba",
            precio=Decimal("100.00"),
            precio_adulto=Decimal("100.00"),
            precio_nino=Decimal("70.00"),
            cupo_maximo=16,
            cupos_disponibles=16,
        )
        salida = SalidaTour.objects.create(
            tour=tour,
            fecha="2030-01-10",
            cupo_maximo=16,
            cupos_disponibles=16,
        )
        self.reserva = Reserva.objects.create(
            usuario=self.owner,
            salida=salida,
            adultos=1,
            ninos=0,
            total_pagar=Decimal("100.00"),
            nombre="Cliente",
            apellidos="Owner",
            correo="owner@example.com",
            telefono="0999999999",
            identificacion="1234567890",
            estado="pendiente",
        )

    def test_checkout_requires_login(self):
        response = self.client.get(reverse("checkout_reserva", args=[self.reserva.id]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_owner_can_open_checkout(self):
        self.client.login(username="owner", password="pass12345")
        response = self.client.get(reverse("checkout_reserva", args=[self.reserva.id]))
        self.assertEqual(response.status_code, 200)

    def test_other_user_cannot_create_paypal_order(self):
        self.client.login(username="other", password="pass12345")
        response = self.client.post(reverse("create_paypal_order", args=[self.reserva.id]))
        self.assertEqual(response.status_code, 403)
