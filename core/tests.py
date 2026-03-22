from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from .models import Destino, Reserva, SalidaTour, SiteVisit, Tour


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


class AgenciaHorarioAgenciaTests(TestCase):
    def setUp(self):
        self.agencia_group = Group.objects.create(name="agencia")
        self.agencia = User.objects.create_user(
            username="agencia1",
            password="pass12345",
            email="agencia@example.com",
        )
        self.agencia.groups.add(self.agencia_group)

        destino = Destino.objects.create(
            nombre="Isabela",
            imagen_url="https://example.com/isabela.jpg",
        )
        self.tour = Tour.objects.create(
            nombre="Tour Agencia",
            destino=destino,
            descripcion="Tour para agencias",
            precio=Decimal("120.00"),
            precio_adulto=Decimal("120.00"),
            precio_nino=Decimal("70.00"),
            cupo_maximo=16,
            cupos_disponibles=16,
            hora_turno_1="08:00",
            hora_turno_2="14:00",
            visible_para_agencias=True,
        )

    def test_agencia_puede_elegir_turno_definido_por_admin(self):
        self.client.login(username="agencia1", password="pass12345")

        response = self.client.post(
            reverse("tour_detalle", args=[self.tour.id]),
            {
                "fecha_agencia": "2030-02-10",
                "hora_turno_agencia": "08:00",
                "adultos": "2",
                "ninos": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("mis_reservas"))

        reserva = Reserva.objects.get(usuario=self.agencia, tipo_reserva="agencia")
        self.assertEqual(str(reserva.salida.fecha), "2030-02-10")
        self.assertEqual(reserva.hora_turno_agencia.strftime("%H:%M"), "08:00")
        self.assertEqual(reserva.salida.hora.strftime("%H:%M"), "08:00")

    def test_agencia_puede_reusar_turno_configurado_si_ya_hay_otra_reserva_agencia(self):
        otra_agencia = User.objects.create_user(
            username="agencia2",
            password="pass12345",
            email="agencia2@example.com",
        )
        otra_agencia.groups.add(self.agencia_group)
        salida = SalidaTour.objects.create(
            tour=self.tour,
            fecha="2030-02-10",
            hora="08:00",
            cupo_maximo=16,
            cupos_disponibles=16,
        )
        Reserva.objects.create(
            usuario=otra_agencia,
            salida=salida,
            adultos=2,
            ninos=0,
            total_pagar=Decimal("0.00"),
            tipo_reserva="agencia",
            nombre="Agencia",
            apellidos="Dos",
            correo="agencia2@example.com",
            telefono="0999999999",
            identificacion="1234567890",
            estado="solicitud_agencia",
            hora_turno_agencia=salida.hora,
        )

        self.client.login(username="agencia1", password="pass12345")
        response = self.client.post(
            reverse("tour_detalle", args=[self.tour.id]),
            {
                "fecha_agencia": "2030-02-10",
                "hora_turno_agencia": "08:00",
                "adultos": "2",
                "ninos": "0",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Reserva.objects.filter(
                salida__tour=self.tour,
                salida__fecha="2030-02-10",
                hora_turno_agencia="08:00",
                tipo_reserva="agencia",
            ).count(),
            2,
        )

    def test_agencia_ve_formulario_aunque_no_existan_salidas(self):
        self.client.login(username="agencia1", password="pass12345")
        response = self.client.get(reverse("tour_detalle", args=[self.tour.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="fecha_agencia"', html=False)
        self.assertContains(response, 'name="hora_turno_agencia"', html=False)
        self.assertNotContains(response, "Sin disponibilidad")


class SiteVisitByIpTests(TestCase):
    def setUp(self):
        destino = Destino.objects.create(
            nombre="Santa Cruz",
            imagen_url="https://example.com/destino.jpg",
        )
        self.tour = Tour.objects.create(
            nombre="Tour Visitas",
            destino=destino,
            descripcion="Tour con contador",
            precio=Decimal("90.00"),
            precio_adulto=Decimal("90.00"),
            precio_nino=Decimal("60.00"),
            cupo_maximo=16,
            cupos_disponibles=16,
            hora_turno_1="08:00",
            visible_para_agencias=True,
        )

    def test_misma_ip_cuenta_una_sola_vez(self):
        url = reverse("home")

        response_1 = self.client.get(url, REMOTE_ADDR="10.0.0.1")
        response_2 = self.client.get(url, REMOTE_ADDR="10.0.0.1")

        self.assertEqual(response_1.status_code, 200)
        self.assertEqual(response_2.status_code, 200)
        self.assertEqual(SiteVisit.objects.count(), 1)
        self.assertContains(response_2, "Visitas:")
        self.assertContains(response_2, ">1<", html=False)

    def test_ips_distintas_incrementan_contador(self):
        url = reverse("home")

        self.client.get(url, REMOTE_ADDR="10.0.0.1")
        response = self.client.get(url, REMOTE_ADDR="10.0.0.2")

        self.assertEqual(SiteVisit.objects.count(), 2)
        self.assertContains(response, "Visitas:")
        self.assertContains(response, ">2<", html=False)

    def test_home_muestra_total_de_ips_unicas(self):
        SiteVisit.objects.create(ip_address="10.0.0.1")
        SiteVisit.objects.create(ip_address="10.0.0.2")

        response = self.client.get(reverse("home"), REMOTE_ADDR="10.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visitas:")
        self.assertContains(response, ">2<", html=False)
