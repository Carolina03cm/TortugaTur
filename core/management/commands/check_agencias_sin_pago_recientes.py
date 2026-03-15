import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from core.models import Reserva
from core.views import ESTADOS_AGENCIA_VISIBLES

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Enviar correo a agencias con pagos pendientes recientes (0-6 dias)"

    def handle(self, *args, **kwargs):
        hoy = timezone.localdate()
        limite = hoy - timedelta(days=7)

        reservas_pendientes = (
            Reserva.objects.filter(fecha_reserva__date__gt=limite)
            .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
            .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
            .select_related("salida__tour", "usuario")
            .order_by("agencia_correo", "fecha_reserva")
        )

        agrupadas = {}
        for reserva in reservas_pendientes:
            if reserva.alerta_7d_agencia_enviada_en:
                continue
            email = (reserva.agencia_correo or "").strip().lower()
            if not email and reserva.usuario and reserva.usuario.email:
                email = (reserva.usuario.email or "").strip().lower()
            if not email:
                continue
            agrupadas.setdefault(email, []).append(reserva)

        enviados = 0
        for email, reservas in agrupadas.items():
            try:
                total_pendiente = sum([r.total_pagar for r in reservas]) if reservas else 0
                subject = f"Pago pendiente: reservas sin pago ({hoy.strftime('%d/%m/%Y')})"

                detalle = []
                for r in reservas:
                    fecha_label = timezone.localtime(r.fecha_reserva).strftime("%d/%m/%Y")
                    tour_nombre = r.salida.tour.nombre if r.salida and r.salida.tour else "Tour"
                    detalle.append(
                        f"- Reserva #{r.id:06d} | {tour_nombre} | {fecha_label} | Total: ${r.total_pagar}"
                    )
                detalle_txt = "\n".join(detalle) if detalle else "- Sin detalle"

                body = (
                    "Hola,\n\n"
                    "Estas reservas de agencia aun no registran pago:\n"
                    f"{detalle_txt}\n\n"
                    f"Deuda total: ${total_pendiente}\n\n"
                    "Por favor coordina el pago con la secretaria o contáctanos si necesitas ayuda.\n\n"
                    "TortugaTur"
                )

                correo = EmailMessage(
                    subject=subject,
                    body=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[email],
                )
                correo.send(fail_silently=False)
                enviados += 1

                now = timezone.now()
                for r in reservas:
                    r.alerta_7d_agencia_enviada_en = now
                    r.save(update_fields=["alerta_7d_agencia_enviada_en"])
            except Exception as e:
                logger.error("Fallo enviando recordatorio reciente a %s: %s", email, e)

        self.stdout.write(self.style.SUCCESS(f"Se enviaron {enviados} recordatorios recientes."))
