import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings
from core.models import Reserva

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = "Verificar reservas de agencias vencidas y marcarlas como incumplidas"

    def handle(self, *args, **kwargs):
        ahora = timezone.now()
        reservas_vencidas = Reserva.objects.filter(
            estado='bloqueada_por_agencia',
            limite_pago_agencia__lt=ahora
        )
        
        count = 0
        for reserva in reservas_vencidas:
            # Revertimos los cupos para dejarlos disponibles de nuevo (opcional, dependiendo de si quieres que la agencia pague obligatoriamente o se liberen los espacios)
            # Como dice "debe cancelar ese valor pendiente", la mantenemos pendiente de pago y no se libera o depende de su regla de negocio.
            # Por ahora "si no se marca confirmado que se envie un correo a la agencia de que imcumplio y debe cancelar ese valor pendiente que tiene"
            reserva.estado = 'pendiente' # Lo cambiamos a pendiente, la penalizamos
            reserva.save(update_fields=['estado'])
            
            # Notificar agencia
            if reserva.usuario and reserva.usuario.email:
                subject = f"Aviso de Incumplimiento: Reserva #{reserva.id:06d}"
                mensaje = (
                    f"Hola {reserva.usuario.first_name},\n\n"
                    f"La reserva con Código VOUCHER {reserva.codigo_agencia} ha expirado el plazo de los 15 días de confirmación.\n"
                    f"Usted ha incumplido y debe cancelar el valor pendiente de ${reserva.total_pagar}.\n\n"
                    "Por favor, inicie sesión y cancele el monto inmediatamente."
                )
                try:
                    send_mail(
                        subject=subject,
                        message=mensaje,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[reserva.usuario.email],
                        fail_silently=True
                    )
                except Exception as e:
                    logger.error(f"Fallo enviando correo de incumplimiento a {reserva.usuario.email}: {e}")
            count += 1

        self.stdout.write(self.style.SUCCESS(f"Se procesaron {count} reservas de agencia vencidas."))
