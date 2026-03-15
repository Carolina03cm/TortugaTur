import logging
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.mail import EmailMessage
from django.db.models import Q
from django.conf import settings

from core.models import Reserva, EmpresaConfig
from core.views import _calcular_limite_pago_agencia, ESTADOS_AGENCIA_VISIBLES
from core.utils import generar_factura_agencia_mensual_pdf

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Enviar recordatorio mensual de pagos pendientes a agencias (7 dias antes de fin de mes)"

    def handle(self, *args, **kwargs):
        hoy = timezone.localdate()
        recordatorio_dt = _calcular_limite_pago_agencia(hoy)
        recordatorio_date = timezone.localtime(recordatorio_dt).date()
        if hoy != recordatorio_date:
            self.stdout.write(self.style.SUCCESS("Hoy no corresponde enviar recordatorios mensuales."))
            return

        inicio_mes = hoy.replace(day=1)
        if hoy.month == 12:
            fin_mes = date(hoy.year + 1, 1, 1) - timedelta(days=1)
        else:
            fin_mes = date(hoy.year, hoy.month + 1, 1) - timedelta(days=1)

        reservas_pendientes = (
            Reserva.objects.filter(
                fecha_reserva__date__range=[inicio_mes, fin_mes],
            )
            .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
            .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
            .select_related("salida__tour", "usuario")
            .order_by("agencia_correo", "fecha_reserva")
        )

        agrupadas = {}
        for reserva in reservas_pendientes:
            email = (reserva.agencia_correo or "").strip().lower()
            if not email and reserva.usuario and reserva.usuario.email:
                email = (reserva.usuario.email or "").strip().lower()
            if not email:
                continue
            agrupadas.setdefault(email, []).append(reserva)

        empresa, _ = EmpresaConfig.objects.get_or_create(id=1, defaults={"nombre_empresa": "TortugaTur"})

        enviados = 0
        for email, reservas in agrupadas.items():
            total_pendiente = sum([r.total_pagar for r in reservas]) if reservas else 0

            subject = f"Recordatorio mensual: pagos pendientes de agencia ({recordatorio_date.strftime('%d/%m/%Y')})"
            mensaje = (
                "Hola,\n\n"
                f"Este es un recordatorio mensual. Al {recordatorio_date.strftime('%d/%m/%Y')} tienes pagos pendientes por reservas realizadas con TortugaTur.\n\n"
                f"Total pendiente: ${total_pendiente}\n\n"
                "Adjuntamos la factura mensual con el detalle completo de las reservas del mes.\n"
                "Por favor coordina el pago en efectivo con la secretaria o contáctanos si necesitas ayuda.\n\n"
                "TortugaTur"
            )
            try:
                agencia_nombre = reservas[0].agencia_nombre if reservas else "Agencia"
                periodo_label = recordatorio_date.strftime("%B %Y")
                pdf_buffer = generar_factura_agencia_mensual_pdf(
                    agencia_nombre,
                    reservas,
                    periodo_label,
                    empresa=empresa,
                )
                correo = EmailMessage(
                    subject=subject,
                    body=mensaje,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[email],
                )
                correo.attach(
                    f"factura_agencia_{recordatorio_date.strftime('%Y%m')}.pdf",
                    pdf_buffer.getvalue(),
                    "application/pdf",
                )
                correo.send(fail_silently=True)
                enviados += 1
            except Exception as e:
                logger.error(f"Fallo enviando recordatorio mensual a {email}: {e}")

        self.stdout.write(self.style.SUCCESS(f"Se enviaron {enviados} recordatorios mensuales."))
