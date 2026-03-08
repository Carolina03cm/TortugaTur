import json
import logging
import hmac
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group, User
from django.core.mail import send_mail, EmailMessage, EmailMultiAlternatives
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from django.utils.crypto import get_random_string
from datetime import timedelta, datetime, time
from django.http import JsonResponse, HttpResponse
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.db import transaction
from django.db.models import Q, Sum, Count
from django.core.paginator import Paginator
from collections import defaultdict
import re
import unicodedata
from .models import Destino, Tour, SalidaTour, Reserva, Pago, Resena, Ticket, EmpresaConfig, Galeria, UserProfile
from .utils import generar_ticket_pdf, generar_actividad_dia_pdf
from .forms import DestinoForm, TourForm, RegistroTuristaForm, ContactoForm, TuristaLoginForm, EmpresaConfigForm

logger = logging.getLogger(__name__)

CHILD_PRICE_0_2 = Decimal("10.00")
CHILD_PRICE_3_5 = Decimal("35.00")
CHILD_PRICE_NORMAL = Decimal("70.00")
GROUP_SECRETARIA = "secretaria"
GROUP_AGENCIA = "agencia"


def _precio_nino_por_edad(edad_nino):
    if edad_nino is None:
        return CHILD_PRICE_NORMAL
    if edad_nino <= 2:
        return CHILD_PRICE_0_2
    if edad_nino <= 5:
        return CHILD_PRICE_3_5
    return CHILD_PRICE_NORMAL


def _calcular_limite_pago_agencia(fecha_salida):
    """
    Regla de pago agencia:
    - Normal: 15 dias desde hoy.
    - Excepcion: si la salida es cercana, maximo hasta 1 dia antes de la salida.
    """
    ahora = timezone.now()
    limite_estandar = ahora + timedelta(days=15)

    fecha_tope = fecha_salida - timedelta(days=1)
    tz = timezone.get_current_timezone()
    limite_por_cercania = timezone.make_aware(datetime.combine(fecha_tope, time(23, 59, 59)), tz)

    # Si reservaron demasiado cerca (ej. salida hoy/manana), damos una ventana corta inmediata.
    if limite_por_cercania <= ahora:
        return ahora + timedelta(hours=1)

    return min(limite_estandar, limite_por_cercania)


def _agenda_actividad(reservas, salidas):
    agenda = defaultdict(list)

    for reserva in reservas:
        if reserva.estado == "cancelada":
            continue
        fecha_ref = reserva.fecha_reserva.date()
        agenda[fecha_ref].append({
            "tipo": "reserva",
            "dt": reserva.fecha_reserva,
            "id": reserva.id,
            "titulo": f"{reserva.nombre} {reserva.apellidos}".strip(),
            "tour": reserva.salida.tour.nombre,
            "estado": reserva.estado,
            "monto": reserva.total_pagar,
        })

    for salida in salidas:
        hora_ref = salida.hora if salida.hora else time.min
        dt_salida = timezone.make_aware(datetime.combine(salida.fecha, hora_ref), timezone.get_current_timezone())
        agenda[salida.fecha].append({
            "tipo": "salida",
            "dt": dt_salida,
            "id": salida.id,
            "titulo": salida.tour.nombre,
            "tour": salida.tour.nombre,
            "estado": f"{salida.cupos_disponibles}/{salida.cupo_maximo} cupos",
            "monto": None,
        })

    resultado = []
    for fecha, items in sorted(agenda.items(), key=lambda x: x[0], reverse=True):
        items_sorted = sorted(items, key=lambda x: x["dt"], reverse=True)
        resultado.append({"fecha": fecha, "eventos": items_sorted})
    return resultado


def _secretaria_actividad_dia(user, fecha):
    reservas_dia = (
        Reserva.objects.filter(creado_por=user, fecha_reserva__date=fecha)
        .exclude(estado="cancelada")
        .select_related("salida__tour")
        .prefetch_related("pagos")
    )
    items = []
    for res in reservas_dia:
        pago_ok = next((p for p in res.pagos.all() if p.estado == "paid"), None)
        items.append({
            "tipo": "reserva",
            "dt": res.fecha_reserva,
            "id": res.id,
            "titulo": f"{res.nombre} {res.apellidos}".strip(),
            "tour": res.salida.tour.nombre,
            "estado": res.estado,
            "monto": res.total_pagar,
            "metodo_pago": pago_ok.get_proveedor_display() if pago_ok else "Pendiente",
            "usuario": user.username,
        })

    return sorted(items, key=lambda x: x["dt"], reverse=True)

# ============================================
# VISTAS PÃšBLICAS
# ============================================

def home(request):
    destinos = Destino.objects.all()
    tours_destacados = Tour.objects.all()[:3]
    currency_code, currency_rate = _currency_context(request)
    for tour in tours_destacados:
        display = _tour_price_display(tour, currency_rate)
        tour.precio_adulto_display = display["adulto"]
        tour.precio_nino_display = display["nino"]

    if request.GET.get('pago') == 'ok':
        from django.contrib import messages
        messages.success(request, "¡Gracias! Tu pago está siendo procesado. El estado de tu reserva se actualizará en unos minutos una vez confirmado.")

    context = {
        "destinos": destinos,
        "tours_destacados": tours_destacados,
        "currency_code": currency_code,
        "currency_options": list(getattr(settings, "CURRENCY_RATES", {}).keys()),
    }

    return render(request, "core/home.html", context)

def tours(request):
    tours = Tour.objects.select_related("destino").all()
    destinos = Destino.objects.all()
    currency_code, currency_rate = _currency_context(request)
    for tour in tours:
        display = _tour_price_display(tour, currency_rate)
        tour.precio_adulto_display = display["adulto"]
        tour.precio_nino_display = display["nino"]

    context = {
        "tours": tours,
        "destinos": destinos,
        "currency_code": currency_code,
        "currency_options": list(getattr(settings, "CURRENCY_RATES", {}).keys()),
    }
    return render(request, "core/tours.html", context)


def lista_tours(request):
    destino_id = request.GET.get("destino")
    fecha = request.GET.get("fecha")
    personas = request.GET.get("personas")

    if not (destino_id and fecha and personas):
        return render(request, "core/lista_tours.html", {"tours_con_salidas": {}})
        
    # Eliminada la generaciÃ³n automÃ¡tica para evitar saturaciÃ³n.
    # Las salidas ahora se crean manualmente o por bulto desde el panel.
    
    salidas_brutas = SalidaTour.objects.filter(
        tour__destino_id=destino_id,
        fecha=fecha,
        cupos_disponibles__gte=int(personas)
    ).select_related('tour').order_by('hora')

    ahora = timezone.now()
    fecha_hoy = ahora.date()
    hora_actual = ahora.time()

    # Agrupamos por Tour y filtramos fechas/horas pasadas
    tours_con_salidas = {}
    for s in salidas_brutas:
        # Invalidar si la fecha de bÃºsqueda ya pasÃ³
        if s.fecha < fecha_hoy:
            continue
        # Invalidar si es hoy y la hora ya pasÃ³
        if s.fecha == fecha_hoy and s.hora and s.hora < hora_actual:
            continue
            
        if s.tour not in tours_con_salidas:
            tours_con_salidas[s.tour] = []
        tours_con_salidas[s.tour].append(s)

    currency_code, currency_rate = _currency_context(request)
    for tour in tours_con_salidas.keys():
        display = _tour_price_display(tour, currency_rate)
        tour.precio_adulto_display = display["adulto"]
        tour.precio_nino_display = display["nino"]

    return render(request, "core/lista_tours.html", {
        "tours_con_salidas": tours_con_salidas,
        "fecha_busqueda": fecha,
        "personas": personas,
        "currency_code": currency_code,
        "currency_options": list(getattr(settings, "CURRENCY_RATES", {}).keys()),
    })

# ============================================
# DETALLE DEL TOUR Y RESERVA (ACTUALIZADO)
# ============================================

def tour_detalle(request, pk):
    tour = get_object_or_404(Tour, pk=pk)
    horarios_agencia = [h for h in [tour.hora_turno_1, tour.hora_turno_2] if h]
    
    
    # Filtrar solo salidas futuras con cupos disponibles (y que no haya pasado la hora si es hoy)
    ahora = timezone.now()
    fecha_hoy = ahora.date()
    hora_actual = ahora.time()
    
    salidas_brutas = SalidaTour.objects.filter(
        tour=tour, 
        cupos_disponibles__gt=0,
        fecha__gte=fecha_hoy
    ).order_by('fecha', 'hora')
    
    salidas = []
    for s in salidas_brutas:
        if s.fecha == fecha_hoy and s.hora and s.hora < hora_actual:
            continue
        salidas.append(s)

    if request.method == "POST":
        # Verificar si es una peticiÃ³n AJAX
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        
        try:
            salida_id = request.POST.get("salida")
            adultos = int(request.POST.get("adultos", 0))
            ninos = int(request.POST.get("ninos", 0))
            edades_ninos_raw = request.POST.getlist("edades_ninos")
            nombre = request.POST.get("nombre", "")
            telefono = request.POST.get("telefono", "")
            identificacion = request.POST.get("identificacion", "")
            edades_ninos = []

            if ninos > 0:
                if len(edades_ninos_raw) != ninos:
                    error_msg = "Debes ingresar la edad de cada nino."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                try:
                    edades_ninos = [int(v) for v in edades_ninos_raw]
                except (TypeError, ValueError):
                    error_msg = "Debes ingresar edades validas para los ninos."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                if any(edad < 0 for edad in edades_ninos):
                    error_msg = "La edad del nino no puede ser negativa."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

            # Validaciones
            usuario_es_agencia = es_agencia(request.user)
            if usuario_es_agencia and _penalizacion_pendiente_agencia(request.user):
                error_msg = "Tienes una penalizacion pendiente por incumplimiento. Debes cancelarla antes de hacer nuevas reservas."
                if is_ajax:
                    return JsonResponse({'error': error_msg}, status=400)
                messages.error(request, error_msg)
                return redirect('tour_detalle', pk=pk)
            
            fecha_agencia = request.POST.get("fecha_agencia")
            hora_turno_agencia_raw = (request.POST.get("hora_turno_agencia") or "").strip()
            hora_turno_agencia = None
            hora_turno_libre = None

            if usuario_es_agencia:
                if not fecha_agencia:
                    error_msg = "Debes seleccionar una fecha."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                if not hora_turno_agencia_raw:
                    error_msg = "Debes seleccionar un horario."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                from datetime import datetime
                try:
                    fecha_obj = datetime.strptime(fecha_agencia, "%Y-%m-%d").date()
                except ValueError:
                    error_msg = "Formato de fecha inválido."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)
                    
                if fecha_obj < fecha_hoy:
                    error_msg = "No puedes seleccionar una fecha en el pasado."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                if not horarios_agencia:
                    error_msg = "Este tour no tiene turnos configurados. Contacta al administrador."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                try:
                    hora_turno_agencia = datetime.strptime(hora_turno_agencia_raw, "%H:%M").time()
                except ValueError:
                    error_msg = "Formato de horario inválido."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                if hora_turno_agencia not in horarios_agencia:
                    error_msg = "El horario seleccionado no corresponde a este tour."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                turno_ocupado_agencia = Reserva.objects.filter(
                    salida__tour=tour,
                    salida__fecha=fecha_obj,
                    hora_turno_agencia=hora_turno_agencia,
                ).exclude(estado="cancelada").exists()
                if turno_ocupado_agencia:
                    error_msg = "Ese turno ya fue tomado por una agencia. Selecciona el otro horario."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                if fecha_obj == fecha_hoy and hora_turno_agencia < hora_actual:
                    error_msg = "Lo sentimos, el horario para este tour ya ha pasado. Por favor selecciona otra fecha u horario."
                    if is_ajax: return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                hora_turno_libre = next((h for h in horarios_agencia if h != hora_turno_agencia), None)

                # Buscar o crear la salida del turno elegido (el otro turno queda registrado en la reserva)
                salida = SalidaTour.objects.filter(tour=tour, fecha=fecha_obj, hora=hora_turno_agencia).first()
                if not salida:
                    salida = SalidaTour.objects.create(
                        tour=tour,
                        fecha=fecha_obj,
                        hora=hora_turno_agencia,
                        cupo_maximo=tour.cupo_maximo or 16,
                        cupos_disponibles=tour.cupo_maximo or 16
                    )
            else:
                if not salida_id:
                    error_msg = "Debes seleccionar una fecha."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                salida = get_object_or_404(SalidaTour, id=salida_id, tour=tour)
                
                # Validar que la fecha y hora seleccionada no haya pasado al momento de enviar POST
                if salida.fecha < fecha_hoy or (salida.fecha == fecha_hoy and salida.hora and salida.hora < hora_actual):
                    error_msg = "Lo sentimos, el horario para este tour ya ha pasado. Por favor selecciona otra fecha u horario."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

            total_personas = adultos + ninos

            if total_personas <= 0:
                error_msg = "Debes seleccionar al menos una persona."
                if is_ajax:
                    return JsonResponse({'error': error_msg}, status=400)
                messages.error(request, error_msg)
                return redirect('tour_detalle', pk=pk)

            if not salida.hay_cupo(adultos, ninos):
                error_msg = "No hay suficientes cupos disponibles para esta salida."
                if is_ajax:
                    return JsonResponse({'error': error_msg}, status=400)
                messages.error(request, error_msg)
                return redirect('tour_detalle', pk=pk)

            # Validar datos obligatorios solo si el usuario estÃ¡ autenticado
            if request.user.is_authenticated and not all([nombre, telefono, identificacion]):
                error_msg = "Completa todos tus datos personales."
                if is_ajax:
                    return JsonResponse({'error': error_msg}, status=400)
                messages.error(request, error_msg)
                return redirect('tour_detalle', pk=pk)

            if usuario_es_agencia:
                if total_personas > 16:
                    error_msg = "Las agencias solo pueden bloquear un máximo de 16 pasajeros por reserva."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                # Calcular total a pagar (adulto/niÃ±o) referencial
                precio_adulto = tour.precio_adulto_final()
                total_ninos = sum(_precio_nino_por_edad(edad) for edad in edades_ninos)
                total_pagar = (adultos * precio_adulto) + total_ninos

                # Calcular fecha limite dinamica (15 dias o 1 dia antes si la salida es cercana)
                fecha_limite = _calcular_limite_pago_agencia(fecha_obj)
                codigo_agencia = request.POST.get("codigo_agencia", "")
                
                if not codigo_agencia:
                    error_msg = "El código de agencia (VOUCHER) es obligatorio."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)
                    
                archivo_agencia = request.FILES.get("archivo_agencia")

                # Crear reserva bloqueada y descontar cupos
                with transaction.atomic():
                    salida = SalidaTour.objects.select_for_update().get(id=salida.id)
                    if salida.cupos_disponibles < total_personas:
                         raise ValueError("Cupos no disponibles")

                    reserva = Reserva.objects.create(
                        usuario=request.user,
                        salida=salida,
                        adultos=adultos,
                        ninos=ninos,
                        total_pagar=total_pagar,
                        nombre=nombre if nombre else request.user.first_name,
                        apellidos="",
                        correo=request.user.email,
                        telefono=telefono,
                        identificacion=identificacion,
                        estado="bloqueada_por_agencia",
                        codigo_agencia=codigo_agencia,
                        archivo_agencia=archivo_agencia,
                        limite_pago_agencia=fecha_limite,
                        hora_turno_agencia=hora_turno_agencia,
                        hora_turno_libre=hora_turno_libre,
                    )

                    # El turno elegido por agencia queda bloqueado por completo
                    salida.cupos_disponibles = 0
                    salida.save(update_fields=["cupos_disponibles"])

                msg = "¡Bloqueo exitoso! Tienes la responsabilidad de confirmar o cancelar esta reserva antes de la fecha límite."
                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'reserva_id': reserva.id,
                        'redirect_url': reverse('mis_reservas') # O una vista de exito
                    })
                else:
                    messages.success(request, msg)
                    return redirect('mis_reservas')

            else:
                # Flujo normal de Turista
                # Calcular total a pagar (adulto/niÃ±o)
                precio_adulto = tour.precio_adulto_final()
                total_ninos = sum(_precio_nino_por_edad(edad) for edad in edades_ninos)
                total_pagar = (adultos * precio_adulto) + total_ninos

                # Crear la reserva con estado PENDIENTE (hasta que pague)
                reserva = Reserva.objects.create(
                    usuario=request.user if request.user.is_authenticated else None,
                    salida=salida,
                    adultos=adultos,
                    ninos=ninos,
                    total_pagar=total_pagar,
                    nombre=nombre if nombre else (request.user.first_name if request.user.is_authenticated else ""),
                    apellidos="",  # Puedes agregar este campo al formulario si quieres
                    correo=request.user.email if request.user.is_authenticated else "",
                    telefono=telefono,
                    identificacion=identificacion,
                    estado="pendiente"  # IMPORTANTE: Pendiente hasta que pague
                )

                # NO descontamos cupos aquÃ­, se descontarÃ¡n despuÃ©s del pago

                # Responder con la URL del checkout
                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'reserva_id': reserva.id,
                        'redirect_url': reverse('checkout_reserva', args=[reserva.id])
                    })
                else:
                    messages.success(request, "Reserva iniciada. Completa el pago para confirmar.")
                    return redirect('checkout_reserva', reserva_id=reserva.id)

        except Exception as e:
            error_msg = f"Error al procesar la reserva: {str(e)}"
            if is_ajax:
                return JsonResponse({'error': error_msg}, status=500)
            messages.error(request, error_msg)
            return redirect('tour_detalle', pk=pk)

    resenas = tour.resenas.select_related("usuario").order_by("-fecha")
    fotos = tour.fotos.all().order_by('-fecha_agregada')

    currency_code, currency_rate = _currency_context(request)
    price_display = _tour_price_display(tour, currency_rate)
    precio_adulto = price_display["adulto"]
    precio_nino = price_display["nino"]

    salida_seleccionada = request.GET.get('salida')

    bloqueos_agencia_qs = (
        Reserva.objects.filter(
            salida__tour=tour,
            hora_turno_agencia__isnull=False,
            salida__fecha__gte=fecha_hoy,
        )
        .exclude(estado="cancelada")
        .values("salida__fecha", "hora_turno_agencia")
    )
    bloqueos_agencia_por_fecha = {}
    for item in bloqueos_agencia_qs:
        fecha_key = item["salida__fecha"].isoformat()
        hora_key = item["hora_turno_agencia"].strftime("%H:%M")
        bloqueos_agencia_por_fecha.setdefault(fecha_key, set()).add(hora_key)

    import json
    return render(request, "core/tour_detalle.html", {
        "tour": tour,
        "salidas": salidas,
        "salida_seleccionada": salida_seleccionada,
        "resenas": resenas,
        "fotos": fotos,
        "currency_code": currency_code,
        "currency_rate": str(currency_rate),
        "precio_adulto": precio_adulto,
        "precio_nino": precio_nino,
        "payment_currency": _currency(),
        "currency_options": list(getattr(settings, "CURRENCY_RATES", {}).keys()),
        "currency_rates_json": json.dumps(getattr(settings, "CURRENCY_RATES", {})),
        "whatsapp_message": f"Hola, quiero informacion del tour {tour.nombre}",
        "user_is_agencia": es_agencia(request.user),
        "agencia_horarios": horarios_agencia,
        "bloqueos_agencia_json": json.dumps({
            f: sorted(list(horas)) for f, horas in bloqueos_agencia_por_fecha.items()
        }),
        "child_price_0_2": str(CHILD_PRICE_0_2),
        "child_price_3_5": str(CHILD_PRICE_3_5),
        "child_price_normal": str(CHILD_PRICE_NORMAL),
    })

@login_required
@require_POST
def crear_resena(request, pk):
    tour = get_object_or_404(Tour, pk=pk)
    comentario = (request.POST.get("comentario") or "").strip()
    try:
        puntuacion = int(request.POST.get("puntuacion", "0"))
    except ValueError:
        puntuacion = 0

    if puntuacion < 1 or puntuacion > 5:
        messages.error(request, "La puntuacion debe estar entre 1 y 5.")
        return redirect("tour_detalle", pk=pk)
    if not comentario:
        messages.error(request, "Escribe un comentario antes de enviar.")
        return redirect("tour_detalle", pk=pk)

    Resena.objects.create(
        usuario=request.user,
        tour=tour,
        puntuacion=puntuacion,
        comentario=comentario,
    )
    messages.success(request, "Gracias por compartir tu experiencia.")
    return redirect("tour_detalle", pk=pk)

def ticket_reserva(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    return render(request, "core/ticket.html", {"reserva": reserva, "empresa": _empresa_config()})

def ver_ticket_pdf(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    buffer = generar_ticket_pdf(reserva, _empresa_config())
    return HttpResponse(buffer.getvalue(), content_type='application/pdf')

# ============================================
# CHECKOUT Y PAGO (ACTUALIZADO)
# ============================================

def checkout(request, reserva_id=None):
    """Vista para la pÃ¡gina de checkout/pago"""
    
    # Si se especifica una reserva, cargar sus datos
    if reserva_id:
        reserva = get_object_or_404(Reserva, id=reserva_id)
        
        context = {
            'reserva': reserva,
            'tour': reserva.salida.tour,
            'salida': reserva.salida,
            'destino': reserva.salida.tour.destino,
        }
    else:
        # Datos de ejemplo para demo (si no hay reserva_id)
        context = {
            'demo': True,
        }
    
    return render(request, 'core/checkout.html', context)

def procesar_pago(request):
    """Vista para procesar el pago y confirmar la reserva"""
    if request.method == 'POST':
        reserva_id = request.POST.get('reserva_id')
        
        if not reserva_id:
            messages.error(request, 'No se encontró la reserva')
            return redirect('tours')
        
        # Obtener los datos del formulario
        nombre_titular = request.POST.get('nombre_titular')
        email = request.POST.get('email')
        numero_tarjeta = request.POST.get('numero_tarjeta')
        cvv = request.POST.get('cvv')
        
        try:
            reserva = get_object_or_404(Reserva, id=reserva_id)
            
            # Verificar que la reserva estÃ© pendiente
            if reserva.estado != 'pendiente':
                messages.warning(request, 'Esta reserva ya fue procesada.')
                return redirect('tours')
            
            # AquÃ­ irÃ­a la integraciÃ³n con pasarela de pago real
            # Por ahora, simulamos que el pago fue exitoso
            
            # Actualizar la reserva a PAGADA
            reserva.estado = 'pagada'
            if email:
                reserva.correo = email.strip().lower()
            reserva.save()
            
            # AHORA SÃ descontamos los cupos
            salida = reserva.salida
            total_personas = reserva.adultos + reserva.ninos
            salida.cupos_disponibles -= total_personas
            salida.save()
            
            # Generar y enviar ticket por email
            try:
                pdf_buffer = generar_ticket_pdf(reserva, _empresa_config())
                pdf_content = pdf_buffer.getvalue()
                pdf_buffer.close()
                
                asunto = f"✅ Confirmación de Reserva #{reserva.id:06d} - TortugaTur"
                mensaje_html = render_to_string("core/email_ticket.html", {"reserva": reserva, "empresa": _empresa_config()})
                
                # Enviar al cliente
                email_cliente = EmailMessage(
                    subject=asunto,
                    body=mensaje_html,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[reserva.correo if reserva.correo else email],
                )
                email_cliente.content_subtype = "html"
                email_cliente.attach(f"Ticket_TortugaTur_{reserva.id}.pdf", pdf_content, "application/pdf")
                email_cliente.send(fail_silently=True)
                
            except Exception as e:
                print(f"Error enviando email: {e}")
            
            messages.success(request, '¡Pago procesado exitosamente! Tu reserva ha sido confirmada. Revisa tu email.')
            return redirect('tours')
            
        except Exception as e:
            messages.error(request, f'Error al procesar el pago: {str(e)}')
            return redirect('checkout_reserva', reserva_id=reserva_id)
    
    messages.error(request, 'Método no permitido')
    return redirect('tours')

# ============================================
# PANEL ADMINISTRATIVO
# ============================================

def es_admin(user):
    return user.is_staff or user.is_superuser

def es_secretaria(user):
    return user.is_authenticated and user.groups.filter(name__iexact=GROUP_SECRETARIA).exists()

def es_agencia(user):
    if not user.is_authenticated:
        return False
    if user.groups.filter(name__iexact=GROUP_AGENCIA).exists():
        return True
    perfil = getattr(user, "perfil", None)
    return bool(perfil and getattr(perfil, "is_agencia", False))

def es_staff_o_secretaria(user):
    return es_admin(user) or es_secretaria(user)

def es_admin_o_secretaria(user):
    return es_staff_o_secretaria(user)


def _puede_gestionar_checkout(user, reserva):
    if not user or not user.is_authenticated:
        return False
    if es_admin_o_secretaria(user):
        return True
    if reserva.usuario_id and reserva.usuario_id == user.id:
        return True
    if reserva.creado_por_id and reserva.creado_por_id == user.id:
        return True
    return False

@login_required
@user_passes_test(es_admin_o_secretaria)
def panel_admin(request):
    _cancelar_reservas_agencia_vencidas()
    # En dashboard principal siempre mostramos actividad/KPIs del dia actual.
    # El admin puede consultar otras fechas desde los modulos de actividad y filtros.
    actividad_fecha = timezone.localdate()

    context = {
        "es_secretaria_panel": es_secretaria(request.user) and not es_admin(request.user),
        "actividad_fecha": actividad_fecha,
        "panel_rol_label": "Administrador" if es_admin(request.user) else "Secretaria",
        "panel_profile_url": "",
    }
    perfil_user = getattr(request.user, "perfil", None)
    if perfil_user and getattr(perfil_user, "foto", None):
        try:
            context["panel_profile_url"] = perfil_user.foto.url
        except Exception:
            context["panel_profile_url"] = ""
    ahora = timezone.now()
    hoy = timezone.localdate()
    context["recordatorios_agencia"] = (
        Reserva.objects.filter(estado="bloqueada_por_agencia", salida__fecha__gte=timezone.localdate())
        .select_related("salida__tour", "usuario")
        .order_by("limite_pago_agencia", "salida__fecha", "hora_turno_agencia")[:12]
    )
    for item in context["recordatorios_agencia"]:
        if item.limite_pago_agencia:
            segundos_restantes = int((item.limite_pago_agencia - ahora).total_seconds())
            item.esta_vencida = segundos_restantes < 0
            item.horas_restantes = max(segundos_restantes // 3600, 0)
        else:
            item.esta_vencida = False
            item.horas_restantes = None
        item.dias_para_salida = (item.salida.fecha - hoy).days
        item.alerta_previa_turno = 0 <= item.dias_para_salida <= 3

    # KPI y alertas operativas del dashboard (por dia seleccionado)
    reservas_hoy = Reserva.objects.filter(fecha_reserva__date=actividad_fecha).exclude(estado="cancelada")
    pagos_hoy = Pago.objects.filter(estado="paid", creado_en__date=actividad_fecha)
    bloqueos_hoy = Reserva.objects.filter(
        estado="bloqueada_por_agencia",
        fecha_reserva__date=actividad_fecha,
    )
    penalizaciones_hoy = Pago.objects.filter(
        estado__in=["created", "approved"],
        payload__tipo="penalizacion_incumplimiento",
        creado_en__date=actividad_fecha,
    )
    alertas_operativas = []
    vence_hoy = Reserva.objects.filter(
        estado="bloqueada_por_agencia",
        limite_pago_agencia__date=actividad_fecha,
    ).count()
    if vence_hoy:
        alertas_operativas.append({
            "titulo": "Bloqueos que vencen hoy",
            "detalle": f"{vence_hoy} reserva(s) de agencia requieren confirmacion hoy.",
            "enlace": reverse("admin_reservas"),
            "nivel": "alto",
        })
    if penalizaciones_hoy.exists():
        total_penal = penalizaciones_hoy.aggregate(total=Sum("monto")).get("total") or Decimal("0.00")
        alertas_operativas.append({
            "titulo": "Penalizaciones pendientes",
            "detalle": f"{penalizaciones_hoy.count()} pendiente(s), total ${total_penal}.",
            "enlace": reverse("admin_reservas"),
            "nivel": "medio",
        })
    alertas_operativas.append({
        "titulo": "Actividad de secretarias",
        "detalle": "Revisa actividad detallada con filtros y paginacion.",
        "enlace": reverse("panel_actividad"),
        "nivel": "info",
    })
    context["kpi_panel"] = {
        "reservas_hoy": reservas_hoy.count(),
        "ingresos_hoy": pagos_hoy.aggregate(total=Sum("monto")).get("total") or Decimal("0.00"),
        "bloqueos_hoy": bloqueos_hoy.count(),
        "penalizaciones_hoy": penalizaciones_hoy.count(),
    }
    context["alertas_operativas"] = alertas_operativas
    if request.user.is_staff or request.user.is_superuser:
        inicio_mes = hoy.replace(day=1)
        inicio_anio = hoy.replace(month=1, day=1)
        ingresos_total = _resumen_ingresos_reservas().get("total") or Decimal("0.00")
        ingresos_mes = _resumen_ingresos_reservas(inicio_mes).get("total") or Decimal("0.00")
        ingresos_anio = _resumen_ingresos_reservas(inicio_anio).get("total") or Decimal("0.00")

        reservas_mes_qs = Reserva.objects.filter(fecha_reserva__date__gte=inicio_mes).exclude(estado="cancelada")
        reservas_anio_qs = Reserva.objects.filter(fecha_reserva__date__gte=inicio_anio).exclude(estado="cancelada")
        inv_mes = reservas_mes_qs.aggregate(adultos=Sum("adultos"), ninos=Sum("ninos"))
        inv_anio = reservas_anio_qs.aggregate(adultos=Sum("adultos"), ninos=Sum("ninos"))

        context["resumen_financiero"] = {
            "ingresos_total": ingresos_total,
            "ingresos_mes": ingresos_mes,
            "ingresos_anio": ingresos_anio,
            "reservas_mes": reservas_mes_qs.count(),
            "reservas_anio": reservas_anio_qs.count(),
            "pasajeros_mes": int(inv_mes.get("adultos") or 0) + int(inv_mes.get("ninos") or 0),
            "pasajeros_anio": int(inv_anio.get("adultos") or 0) + int(inv_anio.get("ninos") or 0),
        }

        secretaria_group = Group.objects.filter(name__iexact=GROUP_SECRETARIA).first()
        if secretaria_group:
            context["actividad_reservas"] = (
                Reserva.objects.filter(
                    creado_por__groups=secretaria_group,
                    fecha_reserva__date=actividad_fecha,
                )
                .exclude(estado="cancelada")
                .select_related("creado_por", "salida__tour")
                .distinct()
                .order_by("-fecha_reserva")[:6]
            )
            context["actividad_salidas"] = (
                SalidaTour.objects.filter(
                    creado_por__groups=secretaria_group,
                    fecha=actividad_fecha,
                )
                .select_related("creado_por", "tour")
                .distinct()
                .order_by("-id")[:6]
            )
            agenda_admin = _agenda_actividad(
                Reserva.objects.filter(creado_por__groups=secretaria_group)
                .exclude(estado="cancelada")
                .select_related("creado_por", "salida__tour")
                .distinct()
                .order_by("-fecha_reserva")[:200],
                SalidaTour.objects.filter(creado_por__groups=secretaria_group)
                .select_related("creado_por", "tour")
                .distinct()
                .order_by("-id")[:200],
            )
            agenda_dia = next((g["eventos"] for g in agenda_admin if g["fecha"] == actividad_fecha), [])
            context["agenda_secretarias_dia"] = agenda_dia[:10]
            context["agenda_secretarias_total"] = len(agenda_dia)
        else:
            context["actividad_reservas"] = []
            context["actividad_salidas"] = []
            context["agenda_secretarias_dia"] = []
            context["agenda_secretarias_total"] = 0
    elif context["es_secretaria_panel"]:
        desde_historial = actividad_fecha - timedelta(days=29)
        reservas_secretaria = (
            Reserva.objects.filter(creado_por=request.user)
            .exclude(estado="cancelada")
            .select_related("salida__tour")
            .prefetch_related("pagos")
            .order_by("-fecha_reserva")
        )
        total_ventas_pagadas = sum((r.total_pagar for r in reservas_secretaria if r.estado == "pagada"), Decimal("0.00"))
        efectivo_cobrado = (
            Pago.objects.filter(
                reserva__creado_por=request.user,
                estado="paid",
                proveedor="efectivo",
            ).aggregate(total=Sum("monto")).get("total") or Decimal("0.00")
        )
        historial_ventas = list(reservas_secretaria[:20])
        for res in historial_ventas:
            pago_ok = next((p for p in res.pagos.all() if p.estado == "paid"), None)
            res.metodo_pago = pago_ok.get_proveedor_display() if pago_ok else "Pendiente"

        context["resumen_secretaria"] = {
            "total_reservas": reservas_secretaria.count(),
            "total_ventas": total_ventas_pagadas,
            "total_efectivo": efectivo_cobrado,
            "total_pasajeros": sum((r.total_personas() for r in reservas_secretaria), 0),
        }
        context["agenda_secretaria_dia"] = _secretaria_actividad_dia(request.user, actividad_fecha)

        # Historial diario de ventas/ingresos para secretaria (ultimos 30 dias).
        resumen_reservas = (
            Reserva.objects.filter(
                creado_por=request.user,
                fecha_reserva__date__gte=desde_historial,
            )
            .exclude(estado="cancelada")
            .values("fecha_reserva__date")
            .annotate(total_reservas=Count("id"))
        )
        resumen_ingresos = (
            Pago.objects.filter(
                reserva__creado_por=request.user,
                estado="paid",
                creado_en__date__gte=desde_historial,
            )
            .values("creado_en__date")
            .annotate(total_ingresos=Sum("monto"))
        )

        historial_map = {}
        for fila in resumen_reservas:
            fecha = fila["fecha_reserva__date"]
            historial_map[fecha] = {
                "fecha": fecha,
                "total_reservas": fila["total_reservas"] or 0,
                "total_ingresos": Decimal("0.00"),
            }
        for fila in resumen_ingresos:
            fecha = fila["creado_en__date"]
            base = historial_map.setdefault(
                fecha,
                {"fecha": fecha, "total_reservas": 0, "total_ingresos": Decimal("0.00")},
            )
            base["total_ingresos"] = fila["total_ingresos"] or Decimal("0.00")

        context["historial_secretaria_dias"] = sorted(
            historial_map.values(),
            key=lambda x: x["fecha"],
            reverse=True,
        )[:30]

        context["kpi_panel"] = {
            "reservas_hoy": Reserva.objects.filter(creado_por=request.user, fecha_reserva__date=actividad_fecha).exclude(estado="cancelada").count(),
            "ingresos_hoy": (
                Pago.objects.filter(
                    reserva__creado_por=request.user,
                    estado="paid",
                    creado_en__date=actividad_fecha,
                ).aggregate(total=Sum("monto")).get("total") or Decimal("0.00")
            ),
            "bloqueos_hoy": Reserva.objects.filter(
                creado_por=request.user,
                estado="bloqueada_por_agencia",
                fecha_reserva__date=actividad_fecha,
            ).count(),
            "penalizaciones_hoy": Pago.objects.filter(
                reserva__creado_por=request.user,
                estado__in=["created", "approved"],
                payload__tipo="penalizacion_incumplimiento",
                creado_en__date=actividad_fecha,
            ).count(),
        }
        context["alertas_operativas"] = [
            {
                "titulo": "Actividad del equipo",
                "detalle": "Usa el modulo de actividad para revisar tus movimientos por fecha.",
                "enlace": reverse("panel_actividad"),
                "nivel": "info",
            }
        ]

    return render(request, "core/panel/index.html", context)


@login_required
@user_passes_test(es_admin)
def panel_actividad_secretarias_fragment(request):
    actividad_fecha_str = (request.GET.get("actividad_fecha") or "").strip()
    try:
        actividad_fecha = datetime.strptime(actividad_fecha_str, "%Y-%m-%d").date() if actividad_fecha_str else timezone.localdate()
    except ValueError:
        actividad_fecha = timezone.localdate()

    agenda_secretarias_dia = []
    secretaria_group = Group.objects.filter(name__iexact=GROUP_SECRETARIA).first()
    if secretaria_group:
        agenda_admin = _agenda_actividad(
            Reserva.objects.filter(creado_por__groups=secretaria_group)
            .exclude(estado="cancelada")
            .select_related("creado_por", "salida__tour")
            .distinct()
            .order_by("-fecha_reserva")[:200],
            SalidaTour.objects.filter(creado_por__groups=secretaria_group)
            .select_related("creado_por", "tour")
            .distinct()
            .order_by("-id")[:200],
        )
        agenda_secretarias_dia = next((g["eventos"] for g in agenda_admin if g["fecha"] == actividad_fecha), [])[:10]

    return render(
        request,
        "core/panel/_actividad_secretarias_panel.html",
        {
            "actividad_fecha": actividad_fecha,
            "agenda_secretarias_dia": agenda_secretarias_dia,
        },
    )


@login_required
@user_passes_test(es_admin_o_secretaria)
def panel_actividad(request):
    _cancelar_reservas_agencia_vencidas()
    hoy = timezone.localdate()
    fecha_desde_str = (request.GET.get("desde") or "").strip()
    fecha_hasta_str = (request.GET.get("hasta") or "").strip()
    tipo = (request.GET.get("tipo") or "").strip().lower()
    if tipo == "salida":
        tipo = ""
    secretaria_id = (request.GET.get("secretaria") or "").strip()

    try:
        fecha_desde = datetime.strptime(fecha_desde_str, "%Y-%m-%d").date() if fecha_desde_str else hoy
    except ValueError:
        fecha_desde = hoy
    try:
        fecha_hasta = datetime.strptime(fecha_hasta_str, "%Y-%m-%d").date() if fecha_hasta_str else hoy
    except ValueError:
        fecha_hasta = hoy
    if fecha_hasta < fecha_desde:
        fecha_hasta = fecha_desde

    items = []
    secretarias = []
    secretaria_group = Group.objects.filter(name__iexact=GROUP_SECRETARIA).first()

    if es_admin(request.user):
        reservas_qs = (
            Reserva.objects.filter(fecha_reserva__date__range=[fecha_desde, fecha_hasta])
            .exclude(estado="cancelada")
            .select_related("salida__tour", "creado_por", "usuario")
            .prefetch_related("pagos")
        )
        if secretaria_group:
            reservas_qs = reservas_qs.filter(creado_por__groups=secretaria_group).distinct()
            secretarias = list(secretaria_group.user_set.filter(is_active=True).order_by("username"))
        if secretaria_id:
            reservas_qs = reservas_qs.filter(creado_por_id=secretaria_id)
    else:
        reservas_qs = (
            Reserva.objects.filter(
                creado_por=request.user,
                fecha_reserva__date__range=[fecha_desde, fecha_hasta],
            )
            .exclude(estado="cancelada")
            .select_related("salida__tour")
            .prefetch_related("pagos")
        )
    if tipo in ["", "reserva"]:
        for res in reservas_qs:
            pago_ok = next((p for p in res.pagos.all() if p.estado == "paid"), None)
            items.append({
                "tipo": "reserva",
                "dt": res.fecha_reserva,
                "id": res.id,
                "titulo": f"{res.nombre} {res.apellidos}".strip(),
                "tour": res.salida.tour.nombre,
                "estado": res.estado,
                "monto": res.total_pagar,
                "metodo_pago": pago_ok.get_proveedor_display() if pago_ok else "Pendiente",
                "usuario": (
                    res.creado_por.username if res.creado_por else (res.usuario.username if res.usuario else "web")
                ),
            })

    items = sorted(items, key=lambda x: x["dt"], reverse=True)
    paginator = Paginator(items, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "core/panel/actividad.html",
        {
            "page_obj": page_obj,
            "secretarias": secretarias,
            "filtro_desde": fecha_desde,
            "filtro_hasta": fecha_hasta,
            "filtro_tipo": tipo,
            "filtro_secretaria": secretaria_id,
            "es_admin_panel": es_admin(request.user),
        },
    )


@login_required
@user_passes_test(es_admin_o_secretaria)
def descargar_actividad_dia_pdf(request):
    actividad_fecha_str = (request.GET.get("actividad_fecha") or "").strip()
    try:
        actividad_fecha = datetime.strptime(actividad_fecha_str, "%Y-%m-%d").date() if actividad_fecha_str else timezone.localdate()
    except ValueError:
        messages.error(request, "Fecha invalida para generar el PDF.")
        return redirect("panel_admin")

    if es_secretaria(request.user) and not es_admin(request.user):
        items = _secretaria_actividad_dia(request.user, actividad_fecha)
        titulo = f"Actividad del dia - Secretaria {request.user.username}"
    else:
        reservas_admin = (
            Reserva.objects.filter(fecha_reserva__date=actividad_fecha)
            .exclude(estado="cancelada")
            .select_related("salida__tour", "creado_por", "usuario")
            .prefetch_related("pagos")
            .distinct()
        )
        items = []
        for res in reservas_admin:
            pago_ok = next((p for p in res.pagos.all() if p.estado == "paid"), None)
            if res.creado_por:
                autor = res.creado_por.username
            elif res.usuario:
                autor = res.usuario.username
            else:
                autor = "web"
            items.append({
                "tipo": "reserva",
                "dt": res.fecha_reserva,
                "id": res.id,
                "titulo": f"{res.nombre} {res.apellidos}".strip(),
                "tour": res.salida.tour.nombre,
                "estado": res.estado,
                "monto": res.total_pagar,
                "metodo_pago": pago_ok.get_proveedor_display() if pago_ok else "Pendiente",
                "usuario": autor,
            })
        items = sorted(items, key=lambda x: x["dt"], reverse=True)
        titulo = "Actividad general del dia"

    resumen = {
        "total_registros": len(items),
        "total_ventas": sum(
            ((item["monto"] or Decimal("0.00")) for item in items if item.get("estado") == "pagada"),
            Decimal("0.00")
        ),
    }
    buffer = generar_actividad_dia_pdf(titulo, actividad_fecha, items, resumen, _empresa_config())
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="actividad_{actividad_fecha.strftime("%Y%m%d")}.pdf"'
    return response

def _empresa_config():
    empresa, _ = EmpresaConfig.objects.get_or_create(id=1, defaults={"nombre_empresa": "TortugaTur"})
    return empresa


@login_required
@user_passes_test(es_admin)
def empresa_config(request):
    empresa = _empresa_config()
    if request.method == "POST":
        form = EmpresaConfigForm(request.POST, instance=empresa)
        if form.is_valid():
            form.save()
            messages.success(request, "Datos de empresa actualizados.")
            return redirect("empresa_config")
    else:
        form = EmpresaConfigForm(instance=empresa)
    return render(request, "core/panel/empresa_config.html", {"form": form})

@login_required
@user_passes_test(es_admin)
def admin_reservas(request):
    _cancelar_reservas_agencia_vencidas()
    for reserva_estado in Reserva.objects.exclude(estado="pagada").prefetch_related("pagos"):
        tiene_pago_reserva = any(
            p.estado == "paid" and (p.payload or {}).get("tipo") != "penalizacion_incumplimiento"
            for p in reserva_estado.pagos.all()
        )
        if tiene_pago_reserva:
            reserva_estado.estado = "pagada"
            reserva_estado.save(update_fields=["estado"])
    
    # Filtros
    fecha_filtro = request.GET.get('fecha')
    
    reservas_query = (
        Reserva.objects.select_related("salida__tour")
        .prefetch_related("pagos")
        .exclude(estado="pendiente")
        .exclude(estado="cancelada")
    )
    
    if fecha_filtro:
        reservas_query = reservas_query.filter(salida__fecha=fecha_filtro)
        
    reservas = reservas_query.order_by("-id")

    hoy = timezone.localdate()
    inicio_mes = hoy.replace(day=1)
    inicio_anio = hoy.replace(month=1, day=1)
    ingresos_total = _resumen_ingresos_reservas().get("total") or Decimal("0.00")
    ingresos_mes = _resumen_ingresos_reservas(inicio_mes).get("total") or Decimal("0.00")
    ingresos_anio = _resumen_ingresos_reservas(inicio_anio).get("total") or Decimal("0.00")
    ingresos_fecha_filtro = None
    if fecha_filtro:
        ingresos_fecha_filtro = (
            Reserva.objects.filter(salida__fecha=fecha_filtro)
            .filter(Q(estado="pagada") | Q(pagos__estado="paid"))
            .exclude(estado="cancelada")
            .distinct()
            .aggregate(total=Sum("total_pagar"))
            .get("total")
            or Decimal("0.00")
        )
    
    for reserva in reservas:
        reserva.es_reserva_agencia = _es_reserva_agencia(reserva)
        reserva.tiene_pago = any(pago.estado == "paid" for pago in reserva.pagos.all())
        pago_exitoso = next((pago for pago in reserva.pagos.all() if pago.estado == "paid"), None)
        penalizacion = next(
            (
                pago for pago in reserva.pagos.all()
                if (pago.payload or {}).get("tipo") == "penalizacion_incumplimiento"
            ),
            None
        )
        reserva.penalizacion_pendiente = bool(penalizacion and penalizacion.estado != "paid")
        reserva.monto_penalizacion = penalizacion.monto if penalizacion else None
        if pago_exitoso:
            reserva.proveedor_pago = pago_exitoso.get_proveedor_display()
        else:
            reserva.proveedor_pago = None
    return render(
        request,
        "core/panel/reservas.html",
        {
            "reservas": reservas,
            "resumen_financiero_reservas": {
                "ingresos_total": ingresos_total,
                "ingresos_mes": ingresos_mes,
                "ingresos_anio": ingresos_anio,
                "ingresos_fecha_filtro": ingresos_fecha_filtro,
            },
        },
    )

@login_required
@user_passes_test(es_admin)
def cambiar_estado_reserva(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if request.method == "POST":
        if _es_reserva_agencia(reserva):
            messages.error(request, "Las reservas de agencia no se pueden modificar manualmente desde admin.")
            return redirect("admin_reservas")
        nuevo_estado = request.POST.get("estado")
        if nuevo_estado in ["pendiente", "confirmada", "cancelada", "pagada", "bloqueada_por_agencia"]:
            estado_anterior = reserva.estado
            if nuevo_estado == "confirmada":
                nuevo_estado = "pagada"

            reserva.estado = nuevo_estado
            reserva.save(update_fields=["estado"])

            if estado_anterior != "cancelada" and nuevo_estado == "cancelada":
                _recalcular_disponibilidad_salida(reserva.salida)

            texto_estado = "pagada" if nuevo_estado == "pagada" else nuevo_estado
            messages.success(request, f"Reserva #{reserva.id} actualizada correctamente a {texto_estado}.")
    return redirect("admin_reservas")


@login_required
@user_passes_test(es_admin)
def admin_agencias(request):
    from django.contrib.auth.models import User
    group_agencia, _ = Group.objects.get_or_create(name=GROUP_AGENCIA)
    credenciales_generadas = request.session.pop("admin_last_agencia_credentials", None)
    usuarios = (
        User.objects.filter(
            Q(groups=group_agencia)
            | Q(perfil__is_agencia=True)
            | Q(username__iexact=(credenciales_generadas or {}).get("usuario", ""))
        )
        .select_related("perfil")
        .distinct()
        .order_by("-date_joined")
    )
    return render(
        request,
        "core/panel/agencias.html",
        {
            "usuarios": usuarios,
            "credenciales_generadas": credenciales_generadas,
        },
    )

@login_required
@user_passes_test(es_admin)
@require_POST
def crear_agencia(request):
    from django.contrib.auth.models import User
    from .models import UserProfile

    embed_mode = (request.GET.get("embed") == "1") or (request.POST.get("embed") == "1")

    def _redir_agencias(to_top=False):
        url = reverse("admin_agencias")
        if embed_mode:
            url = f"{url}?embed=1"
        if to_top:
            url = f"{url}#credenciales-generadas"
        return redirect(url)

    email = (request.POST.get("email") or "").strip().lower()
    first_name = (request.POST.get("nombre") or "").strip()
    cedula = _normalizar_cedula(request.POST.get("cedula"))

    if not first_name or not email or not cedula:
        messages.error(request, "Nombre/empresa, correo y cédula son obligatorios.")
        return _redir_agencias(to_top=True)
    if len(cedula) < 6:
        messages.error(request, "La cédula debe tener al menos 6 caracteres.")
        return _redir_agencias(to_top=True)

    if User.objects.filter(email__iexact=email).exists():
        messages.error(request, "Ese correo electronico ya esta registrado.")
        return _redir_agencias(to_top=True)

    username = _username_unico(_username_agencia_base(first_name))
    password = cedula

    try:
        group_agencia, _ = Group.objects.get_or_create(name=GROUP_AGENCIA)
        user = User.objects.create_user(username=username, email=email, password=password, first_name=first_name)
        perfil, _ = UserProfile.objects.get_or_create(user=user)
        perfil.is_agencia = True
        perfil.cedula = cedula
        perfil.force_password_change = True
        perfil.save()
        user.groups.add(group_agencia)
        request.session["admin_last_agencia_credentials"] = {
            "usuario": username,
            "password": password,
            "correo": email,
            "cedula": cedula,
            "tipo": "nueva",
        }
        messages.success(request, f"Agencia creada. Usuario: {username} | Clave temporal (cédula): {password}")
    except Exception as e:
        messages.error(request, f"Ocurrio un error al crear la agencia: {e}")

    return _redir_agencias(to_top=True)

@login_required
@user_passes_test(es_admin)
@require_POST
def toggle_agencia(request, user_id):
    from .models import UserProfile
    from django.contrib.auth.models import User
    embed_mode = (request.GET.get("embed") == "1") or (request.POST.get("embed") == "1")

    def _redir_agencias():
        url = reverse("admin_agencias")
        if embed_mode:
            url = f"{url}?embed=1"
        return redirect(url)

    group_agencia, _ = Group.objects.get_or_create(name=GROUP_AGENCIA)
    user = get_object_or_404(User, id=user_id)
    perfil, _ = UserProfile.objects.get_or_create(user=user)

    ahora_es_agencia = not user.groups.filter(id=group_agencia.id).exists()
    if ahora_es_agencia:
        user.groups.add(group_agencia)
    else:
        user.groups.remove(group_agencia)

    perfil.is_agencia = ahora_es_agencia
    perfil.save()
    if ahora_es_agencia:
        messages.success(request, f"{user.username} ha sido convertida en Agencia.")
    else:
        messages.warning(request, f"{user.username} perdio sus privilegios de Agencia.")
    return _redir_agencias()


@login_required
@user_passes_test(es_admin)
@require_POST
def eliminar_agencia(request, user_id):
    from django.contrib.auth.models import User
    embed_mode = (request.GET.get("embed") == "1") or (request.POST.get("embed") == "1")

    def _redir_agencias():
        url = reverse("admin_agencias")
        if embed_mode:
            url = f"{url}?embed=1"
        return redirect(url)

    agencia = get_object_or_404(User, id=user_id)
    perfil = getattr(agencia, "perfil", None)
    if not (agencia.groups.filter(name=GROUP_AGENCIA).exists() or (perfil and perfil.is_agencia)):
        messages.error(request, "El usuario seleccionado no pertenece al rol agencia.")
        return _redir_agencias()

    # No eliminar cuentas con historial para proteger trazabilidad del sistema.
    if Reserva.objects.filter(usuario=agencia).exists():
        messages.error(
            request,
            "No se puede eliminar esta agencia porque tiene reservas registradas. Remueve sus permisos o desactiva su acceso.",
        )
        return _redir_agencias()

    agencia.delete()
    messages.success(request, "Agencia eliminada correctamente.")
    return _redir_agencias()

@login_required
@user_passes_test(es_admin)
@require_POST
def eliminar_reserva(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if _es_reserva_agencia(reserva):
        messages.error(request, "Las reservas de agencia no se pueden eliminar manualmente desde admin.")
        return redirect("admin_reservas")
    reserva_id = reserva.id
    nombre = f"{reserva.nombre} {reserva.apellidos}".strip() or "Cliente"
    reserva.delete()
    messages.success(request, f"Reserva #{reserva_id} de {nombre} eliminada correctamente.")
    return redirect("admin_reservas")

@login_required
@user_passes_test(es_admin_o_secretaria)
def admin_salidas(request):
    fecha_filtro = request.GET.get('fecha')
    salidas_query = SalidaTour.objects.select_related("tour")
    solo_lectura = es_secretaria(request.user) and not es_admin(request.user)
    puede_crear_salida = es_admin(request.user) or es_secretaria(request.user)
    puede_gestionar_salidas = es_admin(request.user)
    
    if fecha_filtro:
        salidas_query = salidas_query.filter(fecha=fecha_filtro)
    
    salidas = salidas_query.order_by('-fecha', 'hora')
    return render(request, "core/panel/salidas.html", {
        "salidas": salidas,
        "fecha_filtro": fecha_filtro,
        "solo_lectura": solo_lectura,
        "puede_crear_salida": puede_crear_salida,
        "puede_gestionar_salidas": puede_gestionar_salidas,
    })

@login_required
@user_passes_test(es_admin)
@require_POST
def eliminar_salida(request, salida_id):
    salida = get_object_or_404(SalidaTour, id=salida_id)
    # Solo permitir eliminar si no tiene reservas pagadas
    if salida.reservas.filter(estado="pagada").exists():
        messages.error(request, "No puedes eliminar una salida que ya tiene reservas pagadas.")
    else:
        salida.delete()
        messages.success(request, "La salida ha sido eliminada correctamente.")
    return redirect("admin_salidas")

@login_required
@user_passes_test(es_admin)
@require_POST
def limpiar_salidas_vacias(request):
    # Eliminar salidas que no tengan NINGUNA reserva
    from django.db.models import Count
    # Filtramos las que tienen 0 reservas
    vacias = SalidaTour.objects.annotate(num_reservas=Count('reservas')).filter(num_reservas=0)
    cantidad = vacias.count()
    vacias.delete()
    messages.success(request, f"Se han eliminado {cantidad} salidas sin reservas (vacias).")
    return redirect("admin_salidas")

@login_required
@user_passes_test(es_admin)
def editar_salida(request, salida_id):
    salida = get_object_or_404(SalidaTour, id=salida_id)
    if request.method == "POST":
        salida.cupo_maximo = int(request.POST.get("cupo_maximo"))
        salida.cupos_disponibles = int(request.POST.get("cupos_disponibles"))
        salida.fecha = request.POST.get("fecha")
        hora = request.POST.get("hora")
        salida.hora = hora if hora else None
        salida.duracion = request.POST.get("duracion") or salida.tour.duracion
        salida.save()
        messages.success(request, f"La salida del {salida.fecha} ha sido actualizada.")
        return redirect("admin_salidas")
    return render(request, "core/panel/editar_salida.html", {"salida": salida})

@login_required
@user_passes_test(es_admin_o_secretaria)
def crear_salida(request):
    tours = Tour.objects.all()
    
    if request.method == "POST":
        tour_id = request.POST.get("tour")
        fecha_inicio_str = request.POST.get("fecha")
        fecha_fin_str = request.POST.get("fecha_fin")
        hora_post = request.POST.get("hora")
        ambos_turnos = request.POST.get("ambos_turnos") == "on"
        cupo_maximo = int(request.POST.get("cupo_maximo"))
        duracion = request.POST.get("duracion")
        
        tour = get_object_or_404(Tour, id=tour_id)
        
        from datetime import datetime
        fecha_inicio = datetime.strptime(fecha_inicio_str, '%Y-%m-%d').date()
        
        fechas = [fecha_inicio]
        if fecha_fin_str:
            fecha_fin = datetime.strptime(fecha_fin_str, '%Y-%m-%d').date()
            dias = (fecha_fin - fecha_inicio).days
            for d in range(1, dias + 1):
                fechas.append(fecha_inicio + timedelta(days=d))
        
        salidas_creadas = 0
        for f in fechas:
            # Turnos a crear
            horas = []
            if ambos_turnos:
                if tour.hora_turno_1: horas.append(tour.hora_turno_1)
                if tour.hora_turno_2: horas.append(tour.hora_turno_2)
            elif hora_post:
                horas.append(hora_post)
            
            for h in horas:
                # Evitar duplicados exactos
                if not SalidaTour.objects.filter(tour=tour, fecha=f, hora=h).exists():
                    SalidaTour.objects.create(
                        tour=tour,
                        fecha=f,
                        hora=h,
                        duracion=duracion or tour.duracion,
                        cupo_maximo=cupo_maximo,
                        cupos_disponibles=cupo_maximo,
                        creado_por=request.user
                    )
                    salidas_creadas += 1
        
        messages.success(request, f"¡Se han programado {salidas_creadas} salidas correctamente!")
        return redirect("admin_salidas")

    return render(request, "core/panel/crear_salida.html", {"tours": tours})

@login_required
@user_passes_test(es_admin)
def destinos(request):
    destinos_list = Destino.objects.all().order_by("-id")
    solo_lectura = es_secretaria(request.user) and not es_admin(request.user)

    if request.method == "POST":
        if solo_lectura:
            messages.error(request, "Tu rol solo tiene permiso de lectura en destinos.")
            return redirect("destinos")
        form = DestinoForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Destino agregado con exito.")
            return redirect("destinos")
    else:
        form = DestinoForm() if not solo_lectura else None

    return render(request, "core/panel/destinos.html", {
        "destinos": destinos_list,
        "form": form,
        "solo_lectura": solo_lectura,
    })

@login_required
@user_passes_test(es_admin)
def editar_destino(request, pk):
    destino = get_object_or_404(Destino, pk=pk)
    if request.method == "POST":
        form = DestinoForm(request.POST, instance=destino)
        if form.is_valid():
            form.save()
            messages.success(request, "Destino actualizado con exito.")
            return redirect("destinos")
    else:
        form = DestinoForm(instance=destino)
    return render(request, "core/panel/editar_destino.html", {"form": form, "destino": destino})

@login_required
@user_passes_test(es_admin)
def eliminar_destino(request, pk):
    destino = get_object_or_404(Destino, pk=pk)
    if request.method == "POST":
        destino.delete()
        messages.success(request, "Destino eliminado correctamente.")
    return redirect("destinos")

@login_required
@user_passes_test(es_admin)
def admin_tours(request):
    tours_list = Tour.objects.all().order_by("-id")
    destinos_list = Destino.objects.all()
    solo_lectura = es_secretaria(request.user) and not es_admin(request.user)

    salidas_json = []
    today = timezone.now().date()
    future = today + timezone.timedelta(days=90)
    salidas_calendar = SalidaTour.objects.filter(fecha__range=[today, future]).select_related("tour")

    for s in salidas_calendar:
        salidas_json.append({
            "title": f"{s.tour.nombre} ({s.cupos_disponibles})",
            "start": s.fecha.isoformat(),
            "url": f"/panel/salidas/editar/{s.id}/" if not solo_lectura else "",
            "backgroundColor": "#13B6EC" if s.cupos_disponibles > 5 else "#ef4444",
            "borderColor": "#13B6EC" if s.cupos_disponibles > 5 else "#ef4444",
        })

    if request.method == "POST":
        if solo_lectura:
            messages.error(request, "Tu rol solo tiene permiso de lectura en tours.")
            return redirect("admin_tours")
        form = TourForm(request.POST)
        if form.is_valid():
            tour = form.save(commit=False)
            tour.cupos_disponibles = tour.cupo_maximo
            tour.save()
            messages.success(request, "Tour creado exitosamente.")
            return redirect("admin_tours")
    else:
        form = TourForm() if not solo_lectura else None

    return render(request, "core/panel/tours.html", {
        "salidas_json": salidas_json,
        "tours": tours_list,
        "form": form,
        "destinos": destinos_list,
        "solo_lectura": solo_lectura,
    })


@login_required
@user_passes_test(es_admin)
def editar_tour(request, pk):
    tour = get_object_or_404(Tour, pk=pk)
    if request.method == "POST":
        form = TourForm(request.POST, instance=tour)
        if form.is_valid():
            tour_actualizado = form.save(commit=False)
            # Keep available seats within the updated max seats.
            if tour_actualizado.cupos_disponibles > tour_actualizado.cupo_maximo:
                tour_actualizado.cupos_disponibles = tour_actualizado.cupo_maximo
            tour_actualizado.save()
            messages.success(request, f"Tour '{tour_actualizado.nombre}' actualizado correctamente.")
            return redirect("admin_tours")
    else:
        form = TourForm(instance=tour)

    return render(request, "core/panel/editar_tour.html", {"form": form, "tour": tour})

@login_required
@user_passes_test(es_admin)
def eliminar_tour(request, pk):
    tour = get_object_or_404(Tour, pk=pk)
    if request.method == 'POST':
        nombre = tour.nombre
        tour.delete()
        messages.success(request, f"Tour '{nombre}' eliminado correctamente.")
    return redirect('admin_tours')

# ============================================
# AUTENTICACIÃ“N
# ============================================

def registro(request):
    if request.method == 'POST':
        form = RegistroTuristaForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, f"¡Bienvenido a TortugaTur, {user.first_name}!")
            return redirect('home')
    else:
        form = RegistroTuristaForm()
    return render(request, 'registration/registro.html', {'form': form})

@never_cache
def vista_login(request):
    """Maneja el inicio de sesiÃ³n y la redirecciÃ³n al tour original."""
    if request.user.is_authenticated:
        perfil = getattr(request.user, "perfil", None)
        if perfil and getattr(perfil, "force_password_change", False):
            return redirect("perfil_admin")
        if es_admin_o_secretaria(request.user):
            return redirect("panel_admin")
        return redirect("home")

    next_url = request.GET.get('next', 'home')
    
    if request.method == 'POST':
        form = TuristaLoginForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            messages.success(request, f"¡Qué bueno verte de nuevo, {user.first_name}!")
            perfil = getattr(user, "perfil", None)
            if perfil and getattr(perfil, "force_password_change", False):
                messages.warning(request, "Debes cambiar tu contraseña temporal antes de continuar.")
                return redirect("perfil_admin")
            if es_admin_o_secretaria(user):
                return redirect("panel_admin")
            return redirect(request.POST.get('next', 'home'))
    else:
        form = TuristaLoginForm()
    
    return render(request, 'registration/login.html', {
        'form': form,
        'next': next_url
    })

@never_cache
def vista_logout(request):
    """Cierra la sesiÃ³n y redirige a la pÃ¡gina de inicio."""
    logout(request)
    messages.info(request, "Has cerrado sesión correctamente.")
    return redirect('home')

from django.contrib.auth.decorators import login_required

@login_required
def mis_reservas(request):
    """Vista para que el turista vea su historial de compras/reservas."""
    _cancelar_reservas_agencia_vencidas()
    reservas = list(
        Reserva.objects.filter(usuario=request.user).exclude(estado="pendiente").order_by('-fecha_reserva')
    )
    ahora = timezone.now()
    for reserva in reservas:
        reserva.puede_cancelar_agencia = False
        if _es_reserva_agencia(reserva) and reserva.estado == "bloqueada_por_agencia":
            if reserva.limite_pago_agencia and reserva.limite_pago_agencia > ahora:
                reserva.puede_cancelar_agencia = True
    return render(request, 'core/mis_reservas.html', {'reservas': reservas})


@require_POST
@login_required
def cancelar_reserva_agencia(request, reserva_id):
    _cancelar_reservas_agencia_vencidas()
    reserva = get_object_or_404(Reserva.objects.select_related("salida"), id=reserva_id, usuario=request.user)
    if not _es_reserva_agencia(reserva):
        messages.error(request, "Esta reserva no corresponde a una agencia.")
        return redirect("mis_reservas")
    if reserva.estado != "bloqueada_por_agencia":
        messages.error(request, "Solo puedes cancelar reservas bloqueadas por agencia.")
        return redirect("mis_reservas")
    if reserva.limite_pago_agencia and reserva.limite_pago_agencia <= timezone.now():
        messages.error(request, "El plazo ya vencio. Esta reserva ya no puede cancelarse manualmente.")
        return redirect("mis_reservas")

    reserva.estado = "cancelada"
    reserva.save(update_fields=["estado"])
    _recalcular_disponibilidad_salida(reserva.salida)
    messages.success(request, f"Reserva #{reserva.id:06d} cancelada por la agencia antes del plazo.")
    return redirect("mis_reservas")

# ============================================
# OTRAS PÃGINAS
# ============================================

def nosotros(request):
    return render(request, "core/nosotros.html")

def contacto(request):
    if request.method == "POST":
        form = ContactoForm(request.POST)
        if form.is_valid():
            datos = form.cleaned_data
            
            subject = f"✨ Nuevo Contacto: {datos['asunto']} - {datos['nombre']}"
            html_content = render_to_string('emails/aviso_contacto.html', {
                'nombre': datos['nombre'],
                'email_usuario': datos['email'],
                'asunto_elegido': datos['asunto'],
                'mensaje_texto': datos['mensaje'],
            })
            text_content = strip_tags(html_content)

            try:
                msg = EmailMultiAlternatives(
                    subject, 
                    text_content, 
                    settings.DEFAULT_FROM_EMAIL, 
                    ['tu-correo@gmail.com']
                )
                msg.attach_alternative(html_content, "text/html")
                msg.send()

                messages.success(request, "¡Mensaje enviado con éxito!")
                return redirect('contacto')
            except Exception as e:
                messages.error(request, "Error al enviar el correo.")
    else:
        form = ContactoForm()
    
    return render(request, "core/contacto.html", {'form': form})

def terminos(request):
    return render(request, 'core/terminos_condiciones.html')

def faq(request):
    return render(request, 'core/faq.html')


def checkout_redirect(request):
    messages.info(request, "Primero selecciona un tour para crear una reserva.")
    return redirect("tours")


def _site_url(request=None):
    if request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return getattr(settings, "SITE_URL", "http://127.0.0.1:8000").rstrip("/")


def _currency():
    return getattr(settings, "PAYMENT_DEFAULT_CURRENCY", "USD").upper()

def _currency_context(request):
    rates = getattr(settings, "CURRENCY_RATES", {}) or {}
    default = _currency()
    code = (request.GET.get("currency") or default).upper()
    if code not in rates:
        code = default
    rate = Decimal(str(rates.get(code, 1)))
    return code, rate

def _tour_price_display(tour, currency_rate):
    precio_adulto = tour.precio_adulto_final()
    precio_nino = tour.precio_nino_final()
    return {
        "adulto": precio_adulto * currency_rate,
        "nino": precio_nino * currency_rate,
    }


def _resumen_ingresos_reservas(fecha_desde=None):
    """
    Resumen robusto de ingresos:
    - Incluye reservas en estado 'pagada'
    - Incluye reservas con pagos 'paid' (excepto penalizaciones)
    Esto cubre datos antiguos sin registro de Pago y datos nuevos con pasarela.
    """
    base = (
        Reserva.objects.filter(
            Q(estado="pagada")
            | Q(pagos__estado="paid")
        )
        .exclude(estado="cancelada")
    )
    if fecha_desde:
        base = base.filter(fecha_reserva__date__gte=fecha_desde)
    base = base.distinct()
    return {
        "total": base.aggregate(total=Sum("total_pagar")).get("total") or Decimal("0.00"),
        "reservas": base.count(),
    }


def _amount_minor_units(amount):
    dec = Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(dec * 100)


def _recalcular_disponibilidad_salida(salida):
    """Recalcula cupos en base a reservas activas y bloqueo exclusivo de agencia."""
    hay_bloqueo_agencia = (
        Reserva.objects.filter(salida=salida, hora_turno_agencia__isnull=False)
        .exclude(estado="cancelada")
        .exists()
    )
    if hay_bloqueo_agencia:
        salida.cupos_disponibles = 0
        salida.save(update_fields=["cupos_disponibles"])
        return

    ocupados = (
        Reserva.objects.filter(salida=salida, estado__in=["pagada", "confirmada", "bloqueada_por_agencia"])
        .aggregate(total_adultos=Sum("adultos"), total_ninos=Sum("ninos"))
    )
    total_ocupados = int(ocupados.get("total_adultos") or 0) + int(ocupados.get("total_ninos") or 0)
    salida.cupos_disponibles = max((salida.cupo_maximo or 0) - total_ocupados, 0)
    salida.save(update_fields=["cupos_disponibles"])


def _penalizacion_pendiente_agencia(user):
    if not user or not user.is_authenticated:
        return False
    return Pago.objects.filter(
        reserva__usuario=user,
        estado__in=["created", "approved"],
        payload__tipo="penalizacion_incumplimiento",
    ).exists()


def _es_reserva_agencia(reserva):
    if reserva.codigo_agencia or reserva.hora_turno_agencia:
        return True
    if reserva.estado == "bloqueada_por_agencia":
        return True
    if reserva.usuario and es_agencia(reserva.usuario):
        return True
    return False


def _registrar_penalizacion_incumplimiento(reserva):
    ya_existe = Pago.objects.filter(
        reserva=reserva,
        payload__tipo="penalizacion_incumplimiento",
    ).exists()
    if ya_existe:
        return
    Pago.objects.create(
        reserva=reserva,
        proveedor="efectivo",
        estado="created",
        moneda=_currency(),
        monto=reserva.total_pagar,
        payload={
            "tipo": "penalizacion_incumplimiento",
            "motivo": "No confirmo ni pago dentro del plazo permitido.",
        },
    )


def _enviar_alerta_correo_agencia_24h(reserva):
    if not reserva.limite_pago_agencia:
        return False

    destinatarios = []
    if reserva.correo:
        destinatarios.append(reserva.correo.strip().lower())
    if reserva.usuario and reserva.usuario.email:
        destinatarios.append(reserva.usuario.email.strip().lower())

    destinatarios = [mail for mail in dict.fromkeys(destinatarios) if mail]
    agencia_email = (getattr(settings, "AGENCIA_EMAIL", "") or "").strip().lower()

    if not destinatarios and not agencia_email:
        return False

    tour_nombre = getattr(getattr(reserva.salida, "tour", None), "nombre", "Tour")
    fecha_salida = reserva.salida.fecha.strftime("%d/%m/%Y") if reserva.salida and reserva.salida.fecha else "-"
    hora_turno = reserva.hora_turno_agencia.strftime("%I:%M %p") if reserva.hora_turno_agencia else "-"
    vence = timezone.localtime(reserva.limite_pago_agencia).strftime("%d/%m/%Y %I:%M %p")

    subject = f"Recordatorio 24h: Reserva #{reserva.id:05d} por vencer"
    body = (
        f"Hola,\n\n"
        f"Tu reserva de agencia #{reserva.id:05d} está próxima a vencer en menos de 24 horas.\n\n"
        f"Tour: {tour_nombre}\n"
        f"Fecha de salida: {fecha_salida}\n"
        f"Turno agencia: {hora_turno}\n"
        f"Fecha límite de pago: {vence}\n\n"
        f"Si no confirmas el pago antes del límite, la reserva se cancelará y se registrará la penalización correspondiente.\n\n"
        f"TortugaTur"
    )

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=destinatarios or [agencia_email],
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("No se pudo enviar alerta 24h para la reserva %s", reserva.id)
        return False


def _cancelar_reservas_agencia_vencidas():
    ahora = timezone.now()
    tz = timezone.get_current_timezone()

    # Normaliza reservas existentes: el limite nunca puede pasar de 1 dia antes de la salida.
    reservas_bloqueadas = (
        Reserva.objects.filter(estado="bloqueada_por_agencia")
        .select_related("salida")
    )
    for reserva in reservas_bloqueadas:
        fecha_tope = reserva.salida.fecha - timedelta(days=1)
        limite_por_salida = timezone.make_aware(
            datetime.combine(fecha_tope, time(23, 59, 59)),
            tz,
        )
        if reserva.limite_pago_agencia:
            nuevo_limite = min(reserva.limite_pago_agencia, limite_por_salida)
        else:
            nuevo_limite = min(ahora + timedelta(days=15), limite_por_salida)

        if reserva.limite_pago_agencia != nuevo_limite:
            reserva.limite_pago_agencia = nuevo_limite
            reserva.save(update_fields=["limite_pago_agencia"])

    # Alerta automatica por correo cuando faltan menos de 24h.
    por_vencer_24h = (
        Reserva.objects.filter(
            estado="bloqueada_por_agencia",
            limite_pago_agencia__isnull=False,
            limite_pago_agencia__gt=ahora,
            limite_pago_agencia__lte=ahora + timedelta(hours=24),
            alerta_24h_agencia_enviada_en__isnull=True,
        )
        .select_related("salida__tour", "usuario")
    )
    for reserva in por_vencer_24h:
        if _enviar_alerta_correo_agencia_24h(reserva):
            reserva.alerta_24h_agencia_enviada_en = timezone.now()
            reserva.save(update_fields=["alerta_24h_agencia_enviada_en"])

    vencidas = (
        Reserva.objects.filter(
            estado="bloqueada_por_agencia",
            limite_pago_agencia__isnull=False,
            limite_pago_agencia__lt=ahora,
        )
        .select_related("salida")
    )
    salidas_afectadas = set()
    ids_vencidas = []
    for reserva in vencidas:
        reserva.estado = "cancelada"
        reserva.save(update_fields=["estado"])
        _registrar_penalizacion_incumplimiento(reserva)
        salidas_afectadas.add(reserva.salida_id)
        ids_vencidas.append(reserva.id)

    if salidas_afectadas:
        for salida in SalidaTour.objects.filter(id__in=salidas_afectadas):
            _recalcular_disponibilidad_salida(salida)

    _limpiar_historial_canceladas_agencia_diario()

    return len(ids_vencidas)


def _limpiar_historial_canceladas_agencia_diario():
    """
    Limpieza diaria: elimina del historial las reservas de agencia que
    estén canceladas y pertenezcan a días anteriores.
    """
    hoy = timezone.localdate()
    filtros_agencia = (
        (Q(codigo_agencia__isnull=False) & ~Q(codigo_agencia=""))
        | Q(hora_turno_agencia__isnull=False)
        | Q(usuario__groups__name=GROUP_AGENCIA)
    )
    (
        Reserva.objects.filter(estado="cancelada", fecha_reserva__date__lt=hoy)
        .filter(filtros_agencia)
        .distinct()
        .delete()
    )


def _send_ticket_email(reserva):
    try:
        pdf_buffer = generar_ticket_pdf(reserva, _empresa_config())
        pdf_content = pdf_buffer.getvalue()
        pdf_buffer.close()
        subject = f"Confirmacion de Reserva #{reserva.id:06d} - TortugaTur"
        html_body = render_to_string(
            "core/email_ticket.html",
            {
                "reserva": reserva,
                "empresa": _empresa_config(),
                "site_url": _site_url(request=None),
                "whatsapp_number": getattr(settings, "WHATSAPP_NUMBER", ""),
                "agencia_email": getattr(settings, "AGENCIA_EMAIL", ""),
            },
        )
        recipient = reserva.correo or (reserva.usuario.email if reserva.usuario else "")
        agencia_email = getattr(settings, "AGENCIA_EMAIL", "")
        if not recipient and not agencia_email:
            return

        to_list = [recipient] if recipient else []
        bcc_list = [agencia_email] if agencia_email and agencia_email != recipient else []

        email_cliente = EmailMessage(
            subject=subject,
            body=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to_list or [agencia_email],
            bcc=bcc_list,
        )
        email_cliente.content_subtype = "html"
        email_cliente.attach(f"Ticket_TortugaTur_{reserva.id}.pdf", pdf_content, "application/pdf")
        email_cliente.send(fail_silently=True)
    except Exception:
        logger.exception("No se pudo enviar ticket para la reserva %s", reserva.id)


def _extract_customer_email(proveedor, payload):
    if not isinstance(payload, dict):
        return ""
    if proveedor == "paypal":
        payer = payload.get("payer", {}) or {}
        return (payer.get("email_address") or "").strip().lower()
    if proveedor == "lemonsqueezy":
        data = payload.get("data", {}) or {}
        attributes = data.get("attributes", {}) or {}
        if attributes.get("user_email"):
            return (attributes.get("user_email") or "").strip().lower()
        first_order_item = (attributes.get("first_order_item") or {}) if isinstance(attributes, dict) else {}
        return (first_order_item.get("user_email") or "").strip().lower()
    return ""


def _mark_reserva_paid(reserva_id, proveedor, external_id="", payload=None):
    with transaction.atomic():
        reserva = Reserva.objects.select_for_update().select_related("salida").get(id=reserva_id)
        salida = SalidaTour.objects.select_for_update().get(id=reserva.salida_id)
        if (
            reserva.estado == "bloqueada_por_agencia"
            and reserva.limite_pago_agencia
            and reserva.limite_pago_agencia < timezone.now()
        ):
            reserva.estado = "cancelada"
            reserva.save(update_fields=["estado"])
            _recalcular_disponibilidad_salida(salida)
            raise ValueError("La reserva fue cancelada por incumplimiento del plazo de pago.")
        customer_email = _extract_customer_email(proveedor, payload or {})
        pago = None
        if external_id:
            pago = (
                Pago.objects.select_for_update()
                .filter(reserva=reserva, proveedor=proveedor, external_id=external_id)
                .order_by("-id")
                .first()
            )
        if pago is None:
            pago = (
                Pago.objects.select_for_update()
                .filter(reserva=reserva, proveedor=proveedor, estado__in=["created", "approved"])
                .order_by("-id")
                .first()
            )

        if reserva.estado == "pagada":
            if customer_email and reserva.correo != customer_email:
                reserva.correo = customer_email
                reserva.save(update_fields=["correo"])
            if pago and pago.estado != "paid":
                pago.estado = "paid"
                pago.payload = payload or pago.payload
                if external_id:
                    pago.external_id = external_id
                pago.save(update_fields=["estado", "payload", "external_id", "actualizado_en"])
            return reserva, False
        if reserva.estado == "cancelada":
            raise ValueError("La reserva esta cancelada.")

        estado_anterior = reserva.estado

        personas = reserva.adultos + reserva.ninos
        # IMPORTANTE: No restar cupos si ya se restaron cuando la agencia bloqueÃ³
        if estado_anterior != "bloqueada_por_agencia":
            if salida.cupos_disponibles < personas:
                raise ValueError("No hay cupos suficientes al confirmar el pago.")

        reserva.estado = "pagada"
        if customer_email:
            reserva.correo = customer_email
            reserva.save(update_fields=["estado", "correo"])
        else:
            reserva.save(update_fields=["estado"])

        if estado_anterior != "bloqueada_por_agencia":
            salida.cupos_disponibles -= personas
            salida.save(update_fields=["cupos_disponibles"])

        if pago:
            pago.estado = "paid"
            pago.moneda = pago.moneda or _currency()
            pago.monto = reserva.total_pagar
            pago.payload = payload or pago.payload
            if external_id:
                pago.external_id = external_id
            pago.save(update_fields=["estado", "moneda", "monto", "payload", "external_id", "actualizado_en"])
        else:
            Pago.objects.create(
                reserva=reserva,
                proveedor=proveedor,
                estado="paid",
                moneda=_currency(),
                monto=reserva.total_pagar,
                external_id=external_id,
                payload=payload or {},
            )

    _send_ticket_email(reserva)
    
    # Enviar correo adicional confirmando que el valor bloqueado fue cancelado si era agencia
    if estado_anterior == "bloqueada_por_agencia" and reserva.usuario and reserva.usuario.email:
        from django.core.mail import send_mail
        from django.template.loader import render_to_string
        subject = f"Confirmación de Pago a Agencia - Reserva #{reserva.id:06d}"
        msg_plain = f"Gracias por su pago. La reserva del código {reserva.codigo_agencia} ha sido procesada."
        send_mail(
            subject=subject,
            message=msg_plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[reserva.usuario.email],
            fail_silently=True
        )

    return reserva, True


def _paypal_base_url():
    env = getattr(settings, "PAYPAL_ENV", "sandbox").lower()
    return "https://api-m.paypal.com" if env == "live" else "https://api-m.sandbox.paypal.com"


def _paypal_access_token():
    client_id = getattr(settings, "PAYPAL_CLIENT_ID", "")
    client_secret = getattr(settings, "PAYPAL_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise ValueError("PayPal no esta configurado.")

    response = requests.post(
        f"{_paypal_base_url()}/v1/oauth2/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _paypal_verify_webhook(request, event_body):
    webhook_id = getattr(settings, "PAYPAL_WEBHOOK_ID", "")
    if not webhook_id:
        return False

    token = _paypal_access_token()
    verify_payload = {
        "transmission_id": request.headers.get("PAYPAL-TRANSMISSION-ID", ""),
        "transmission_time": request.headers.get("PAYPAL-TRANSMISSION-TIME", ""),
        "cert_url": request.headers.get("PAYPAL-CERT-URL", ""),
        "auth_algo": request.headers.get("PAYPAL-AUTH-ALGO", ""),
        "transmission_sig": request.headers.get("PAYPAL-TRANSMISSION-SIG", ""),
        "webhook_id": webhook_id,
        "webhook_event": event_body,
    }
    response = requests.post(
        f"{_paypal_base_url()}/v1/notifications/verify-webhook-signature",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=verify_payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json().get("verification_status") == "SUCCESS"


def _lemonsqueezy_api_base_url():
    return "https://api.lemonsqueezy.com/v1"


def _lemonsqueezy_headers():
    api_key = getattr(settings, "LEMONSQUEEZY_API_KEY", "")
    if not api_key:
        raise ValueError("Lemon Squeezy no esta configurado.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }


def _lemonsqueezy_verify_signature(request):
    secret = getattr(settings, "LEMONSQUEEZY_WEBHOOK_SECRET", "")
    signature = request.headers.get("X-Signature", "")
    if not secret or not signature:
        logger.warning("Webhook Lemon Squeezy sin secret o firma.")
        return False
    digest = hmac.new(secret.encode("utf-8"), request.body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, signature):
        logger.warning("Webhook Lemon Squeezy con firma invalida.")
        return False
    return True


@login_required
def checkout_pago(request, reserva_id):
    _cancelar_reservas_agencia_vencidas()
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if not _puede_gestionar_checkout(request.user, reserva):
        messages.error(request, "No tienes permiso para acceder a este checkout.")
        return redirect("home")
    embed_mode = (request.GET.get("embed") == "1")
    if reserva.estado == "pagada":
        messages.success(request, "Pago confirmado. Tu reserva ya esta pagada.")
        if embed_mode:
            if es_secretaria(request.user) and not es_admin(request.user):
                return redirect(f"{reverse('secretaria_reservar')}?embed=1")
            return redirect(f"{reverse('admin_reservas')}?embed=1")
        return redirect("home")

    context = {
        "reserva": reserva,
        "tour": reserva.salida.tour,
        "salida": reserva.salida,
        "destino": reserva.salida.tour.destino,
        "payment_currency": _currency(),
        "paypal_client_id": getattr(settings, "PAYPAL_CLIENT_ID", ""),
        "lemonsqueezy_enabled": bool(getattr(settings, "LEMONSQUEEZY_API_KEY", "")),
        "paypal_enabled": bool(getattr(settings, "PAYPAL_CLIENT_ID", "")),
    }
    return render(request, "core/checkout.html", context)


@require_POST
@login_required
def create_lemonsqueezy_checkout(request, reserva_id):
    _cancelar_reservas_agencia_vencidas()
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if not _puede_gestionar_checkout(request.user, reserva):
        messages.error(request, "No tienes permiso para iniciar este pago.")
        return redirect("home")
    if reserva.estado not in ["pendiente", "bloqueada_por_agencia"]:
        messages.warning(request, "Esta reserva ya no esta pendiente de pago.")
        return redirect("tours")

    store_id = getattr(settings, "LEMONSQUEEZY_STORE_ID", "")
    variant_id = reserva.salida.tour.lemonsqueezy_variant_id or getattr(settings, "LEMONSQUEEZY_VARIANT_ID", "")
    if not store_id or not variant_id:
        messages.error(request, "Lemon Squeezy no esta configurado.")
        return redirect("checkout_reserva", reserva_id=reserva.id)

    site_url = _site_url(request)
    currency = _currency()
    custom_price = _amount_minor_units(reserva.total_pagar)
    checkout_payload = {
        "data": {
            "type": "checkouts",
            "attributes": {
                "custom_price": custom_price,
                "checkout_data": {
                    "custom": {
                        "reserva_id": str(reserva.id),
                    },
                },
                "checkout_options": {
                    "embed": False,
                },
                "product_options": {
                    "redirect_url": f"{site_url}{reverse('home')}?pago=ok",
                    "receipt_button_text": "Volver a TortugaTur",
                    "receipt_link_url": f"{site_url}{reverse('home')}",
                },
            },
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(store_id)}},
                "variant": {"data": {"type": "variants", "id": str(variant_id)}},
            },
        }
    }
    try:
        response = requests.post(
            f"{_lemonsqueezy_api_base_url()}/checkouts",
            headers=_lemonsqueezy_headers(),
            json=checkout_payload,
            timeout=20,
        )
    except requests.RequestException:
        logger.exception("Error de red al crear checkout Lemon Squeezy para reserva %s", reserva.id)
        messages.error(request, "No se pudo conectar con Lemon Squeezy.")
        return redirect("checkout_reserva", reserva_id=reserva.id)

    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}

    if response.status_code >= 400:
        logger.error("Lemon Squeezy error %s: %s", response.status_code, data)
        error_detail = ""
        if isinstance(data, dict):
            errors = data.get("errors", [])
            if errors and isinstance(errors, list):
                first = errors[0]
                error_detail = first.get("detail") or first.get("title") or ""
        msg = "No se pudo crear el checkout en Lemon Squeezy."
        if error_detail:
            msg = f"{msg} {error_detail}"
        messages.error(request, msg)
        return redirect("checkout_reserva", reserva_id=reserva.id)

    checkout_data = data.get("data", {})
    attributes = checkout_data.get("attributes", {})
    checkout_url = attributes.get("url", "")
    checkout_id = checkout_data.get("id", "")
    if not checkout_url:
        messages.error(request, "Lemon Squeezy no devolvio URL de pago.")
        return redirect("checkout_reserva", reserva_id=reserva.id)

    Pago.objects.create(
        reserva=reserva,
        proveedor="lemonsqueezy",
        estado="created",
        moneda=currency,
        monto=reserva.total_pagar,
        external_id=checkout_id,
        checkout_url=checkout_url,
        payload=data,
    )
    if getattr(settings, "FORCE_EMAIL_ON_CREATED", False):
        _send_ticket_email(reserva)
    return redirect(checkout_url, permanent=False)


@require_POST
@login_required
def create_paypal_order(request, reserva_id):
    _cancelar_reservas_agencia_vencidas()
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if not _puede_gestionar_checkout(request.user, reserva):
        return JsonResponse({"error": "No autorizado para esta reserva."}, status=403)
    if reserva.estado not in ["pendiente", "bloqueada_por_agencia"]:
        return JsonResponse({"error": "La reserva ya no esta pendiente de pago."}, status=400)

    currency = _currency()
    token = _paypal_access_token()
    amount_str = Decimal(reserva.total_pagar).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": str(reserva.id),
                "reference_id": str(reserva.id),
                "amount": {"currency_code": currency, "value": f"{amount_str}"},
                "description": f"Reserva TortugaTur #{reserva.id}",
            }
        ],
        "application_context": {
            "brand_name": "TortugaTur",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "PAY_NOW",
        },
    }
    response = requests.post(
        f"{_paypal_base_url()}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=20,
    )
    body = response.json()
    if response.status_code >= 400:
        return JsonResponse({"error": "No se pudo crear la orden de PayPal.", "details": body}, status=400)

    order_id = body.get("id", "")
    Pago.objects.create(
        reserva=reserva,
        proveedor="paypal",
        estado="created",
        moneda=currency,
        monto=reserva.total_pagar,
        external_id=order_id,
        payload=body,
    )
    if getattr(settings, "FORCE_EMAIL_ON_CREATED", False):
        _send_ticket_email(reserva)
    return JsonResponse({"orderID": order_id})


@require_POST
@login_required
def capture_paypal_order(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if not _puede_gestionar_checkout(request.user, reserva):
        return JsonResponse({"error": "No autorizado para esta reserva."}, status=403)
    try:
        body = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON invalido."}, status=400)

    order_id = body.get("orderID")
    if not order_id:
        return JsonResponse({"error": "orderID es requerido."}, status=400)

    token = _paypal_access_token()
    response = requests.post(
        f"{_paypal_base_url()}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=20,
    )
    data = response.json()
    if response.status_code >= 400:
        return JsonResponse({"error": "No se pudo capturar la orden.", "details": data}, status=400)

    if data.get("status") != "COMPLETED":
        return JsonResponse({"error": f"Estado inesperado: {data.get('status')}", "details": data}, status=400)

    try:
        _mark_reserva_paid(reserva.id, "paypal", external_id=order_id, payload=data)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    return JsonResponse({"ok": True, "redirect_url": reverse("home")})


@csrf_exempt
def lemonsqueezy_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    if not _lemonsqueezy_verify_signature(request):
        return HttpResponse(status=400)

    try:
        event = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    event_name = event.get("meta", {}).get("event_name", "")
    if event_name in ("order_created", "order_refunded"):
        data = event.get("data", {})
        attributes = data.get("attributes", {})
        custom = event.get("meta", {}).get("custom_data", {}) or {}
        reserva_id = custom.get("reserva_id")
        order_id = str(data.get("id", ""))

        if reserva_id and event_name == "order_created":
            try:
                _mark_reserva_paid(
                    int(reserva_id),
                    "lemonsqueezy",
                    external_id=order_id,
                    payload=event,
                )
            except Exception:
                logger.exception("Fallo confirmando pago Lemon Squeezy para reserva %s", reserva_id)
                return HttpResponse(status=500)
        elif reserva_id and event_name == "order_refunded":
            Pago.objects.filter(
                reserva_id=int(reserva_id),
                proveedor="lemonsqueezy",
            ).update(estado="failed", payload=event)
    return HttpResponse(status=200)


@csrf_exempt
def paypal_webhook(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        body = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    try:
        if not _paypal_verify_webhook(request, body):
            return HttpResponse(status=400)
    except Exception:
        logger.exception("Error verificando webhook PayPal")
        return HttpResponse(status=400)

    if body.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        resource = body.get("resource", {})
        order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id", "")
        reserva_id = resource.get("custom_id", "")

        if not reserva_id and order_id:
            try:
                token = _paypal_access_token()
                order_response = requests.get(
                    f"{_paypal_base_url()}/v2/checkout/orders/{order_id}",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    timeout=20,
                )
                order_response.raise_for_status()
                order_data = order_response.json()
                purchase_units = order_data.get("purchase_units", [])
                if purchase_units:
                    reserva_id = purchase_units[0].get("custom_id", "")
            except Exception:
                logger.exception("No se pudo resolver custom_id desde orden %s", order_id)

        if reserva_id:
            try:
                _mark_reserva_paid(int(reserva_id), "paypal", external_id=order_id or resource.get("id", ""), payload=body)
            except Exception:
                logger.exception("Fallo confirmando webhook PayPal para reserva %s", reserva_id)
                return HttpResponse(status=500)
    return HttpResponse(status=200)

def galeria_view(request):
    from .models import Galeria
    fotos = Galeria.objects.all().order_by('-fecha_agregada')
    return render(request, 'core/galeria.html', {'fotos': fotos})

@login_required
@user_passes_test(es_admin)
def panel_galeria(request):
    from .models import Galeria
    from .forms import GaleriaForm
    fotos_list = Galeria.objects.all().order_by('-id')
    
    if request.method == 'POST':
        form = GaleriaForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "¡Imagen agregada a la galería con éxito!")
            return redirect('panel_galeria')
        else:
            messages.error(request, "Error al subir la imagen. Verifica los datos.")
    else:
        form = GaleriaForm()
            
    return render(request, 'core/panel/galeria.html', {
        'fotos': fotos_list,
        'form': form
    })

@login_required
@user_passes_test(es_admin)
@require_POST
def eliminar_galeria(request, pk):
    from .models import Galeria
    foto = get_object_or_404(Galeria, pk=pk)
    foto.delete()
    messages.success(request, "Imagen eliminada correctamente.")
    return redirect('panel_galeria')

@login_required
def perfil_admin(request):
    from .models import UserProfile
    from django.contrib.auth import update_session_auth_hash
    
    # Verificar si el usuario es secretaria
    is_secretaria = request.user.groups.filter(name__iexact=GROUP_SECRETARIA).exists()
    
    # Si es secretaria y estÃ¡ inactivo, mostrar mensaje
    if is_secretaria and not request.user.is_active:
        messages.error(request, "Tu cuenta de secretaria ha sido desactivada. Por favor, contacta al administrador.")
        return redirect('home')
    
    # Aseguramos que el usuario tiene un perfil asociado
    perfil, _ = UserProfile.objects.get_or_create(user=request.user)

    if request.method == "POST":
        if is_secretaria:
            # Secretaria: perfil de solo lectura. Solo se permite el cambio
            # obligatorio de contraseña temporal cuando aplique.
            if not perfil.force_password_change:
                messages.error(request, "Tu perfil es solo lectura. No tienes permisos para modificar datos.")
                return redirect("perfil_admin")

            new_password = (request.POST.get("new_password") or "").strip()
            if not new_password:
                messages.error(request, "Debes establecer una nueva contraseña para continuar.")
                return redirect("perfil_admin")

            request.user.set_password(new_password)
            request.user.save()
            perfil.force_password_change = False
            perfil.save(update_fields=["force_password_change"])
            logout(request)
            messages.success(request, "Contraseña actualizada. Inicia sesión nuevamente con tu nueva clave.")
            return redirect("login")

        # Si está forzado a cambiar contraseña, no permitir guardar sin nueva clave.
        if perfil.force_password_change and not (request.POST.get("new_password") or "").strip():
            messages.error(request, "Debes establecer una nueva contraseña para continuar.")
            return redirect("perfil_admin")

        # Info Basica
        request.user.first_name = request.POST.get('first_name', request.user.first_name)
        request.user.last_name = request.POST.get('last_name', request.user.last_name)
        request.user.email = request.POST.get('email', request.user.email)
        
        # Validacion del nombre de usuario para no chocar (naive)
        new_username = request.POST.get('username')
        if new_username and new_username != request.user.username:
            from django.contrib.auth.models import User
            if not User.objects.filter(username=new_username).exists():
                request.user.username = new_username
            else:
                messages.error(request, "Ese nombre de usuario ya está ocupado.")
                return redirect('perfil_admin')
        
        request.user.save()
        
        # Opciones extra (Foto, TelÃ©fono, BiografÃ­a)
        if 'foto' in request.FILES:
            perfil.foto = request.FILES['foto']
        perfil.telefono = request.POST.get('telefono', perfil.telefono)
        perfil.biografia = request.POST.get('biografia', perfil.biografia)
        perfil.save()
        
        # Cambio de contraseÃ±a si se proporcionÃ³ una
        new_password = request.POST.get('new_password')
        if new_password:
            era_forzada = bool(perfil.force_password_change)
            request.user.set_password(new_password)
            request.user.save()
            perfil.force_password_change = False
            perfil.save(update_fields=["force_password_change"])
            if era_forzada:
                logout(request)
                messages.success(request, "Contraseña actualizada. Inicia sesión nuevamente con tu nueva clave.")
                return redirect("login")
            update_session_auth_hash(request, request.user) # Evita que se cierre sesiÃ³n
            messages.success(request, "¡Contraseña actualizada!")
            
        messages.success(request, "Perfil guardado con éxito.")
        return redirect('perfil_admin')

    # Si es secretaria, obtener sus reservas
    reservas_creadas = []
    total_ventas = Decimal('0.00')
    total_personas = 0
    
    if is_secretaria:
        reservas_creadas = (
            Reserva.objects
            .filter(creado_por=request.user)
            .exclude(estado="cancelada")
            .select_related('salida__tour')
            .order_by('-fecha_reserva')
        )
        total_ventas = sum(r.total_pagar for r in reservas_creadas)
        total_personas = sum(r.total_personas() for r in reservas_creadas)
    
    return render(request, 'core/perfil_admin.html', {
        'perfil': perfil,
        'force_password_change': perfil.force_password_change,
        'is_secretaria': is_secretaria,
        'reservas_creadas': reservas_creadas,
        'total_ventas': total_ventas,
        'total_personas': total_personas,
    })

#secretaria
def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _slug_login_base(texto, fallback="usuario"):
    raw = (texto or "").strip().lower()
    if not raw:
        raw = fallback
    norm = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    norm = re.sub(r"[^a-z0-9]+", ".", norm).strip(".")
    if not norm:
        norm = fallback
    if norm[0].isdigit():
        norm = f"u.{norm}"
    return norm[:24]


def _username_unico(base):
    base = _slug_login_base(base)
    if not User.objects.filter(username__iexact=base).exists():
        return base
    idx = 2
    while True:
        candidato = f"{base[:20]}{idx}"
        if not User.objects.filter(username__iexact=candidato).exists():
            return candidato
        idx += 1


def _password_temporal():
    return get_random_string(10, allowed_chars="abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789")


def _username_secretaria_base(first_name, last_name):
    nombres = [p for p in _slug_login_base(first_name, fallback="s").split(".") if p]
    apellidos = [p for p in _slug_login_base(last_name, fallback="").split(".") if p]
    inicial_nombre = nombres[0][0] if nombres else "s"
    apellido_principal = apellidos[0] if apellidos else ""
    inicial_segundo_apellido = apellidos[1][0] if len(apellidos) > 1 and apellidos[1] else ""
    base = f"{inicial_nombre}{apellido_principal}{inicial_segundo_apellido}"
    return base if base else "secretaria"


def _username_agencia_base(nombre_empresa):
    partes = [p for p in _slug_login_base(nombre_empresa, fallback="agencia").split(".") if p]
    if not partes:
        return "agencia"
    if len(partes) == 1:
        token = partes[0]
        return token[:12] if len(token) > 12 else token
    # Formato corto: inicial de la primera palabra + complemento del resto.
    inicial = partes[0][0]
    complemento = "".join(partes[1:])
    base = f"{inicial}{complemento}"
    return base[:20] if base else "agencia"


def _normalizar_cedula(raw):
    cedula = re.sub(r"[^0-9A-Za-z]", "", (raw or "").strip())
    return cedula

def puede_reservar_asistida(user):
    return es_staff_o_secretaria(user)


@login_required
@user_passes_test(puede_reservar_asistida)
def secretaria_reservar(request):
    embed_mode = (request.GET.get("embed") == "1") or (request.POST.get("embed") == "1")
    def _redir_secretaria_reservar():
        url = reverse("secretaria_reservar")
        if embed_mode:
            url = f"{url}?embed=1"
        return redirect(url)

    destinos = Destino.objects.all().order_by("nombre")
    todos_los_tours = list(Tour.objects.select_related("destino").all().order_by("nombre"))
    tours_reserva_directa = {}
    destino_id = request.GET.get("destino", "")
    fecha = request.GET.get("fecha", "")
    fecha_display = fecha
    try:
        if fecha:
            fecha_display = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        fecha_display = fecha

    ahora = timezone.now()
    fecha_hoy = ahora.date()
    hora_actual = ahora.time()
    salidas_directas = (
        SalidaTour.objects.filter(
            fecha__gte=fecha_hoy,
            cupos_disponibles__gt=0,
        )
        .select_related("tour", "tour__destino")
        .order_by("tour__nombre", "fecha", "hora")
    )
    for salida in salidas_directas:
        if salida.fecha == fecha_hoy and salida.hora and salida.hora < hora_actual:
            continue
        tours_reserva_directa.setdefault(salida.tour, []).append(salida)

    tours_con_salidas = {}
    destino_seleccionado = None
    if destino_id and fecha:
        fecha_solicitada = None
        try:
            fecha_solicitada = datetime.strptime(fecha, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "La fecha de busqueda no es valida.")

        if fecha_solicitada and fecha_solicitada < fecha_hoy:
            messages.warning(request, "La fecha seleccionada ya vencio. Elige hoy o una fecha futura.")
        else:
            destino_seleccionado = Destino.objects.filter(id=destino_id).first()
            if destino_seleccionado:
                salidas = (
                    SalidaTour.objects.filter(
                        tour__destino=destino_seleccionado,
                        fecha=fecha,
                        cupos_disponibles__gt=0,
                    )
                    .select_related("tour", "tour__destino")
                    .order_by("tour__nombre", "hora")
                )
                for salida in salidas:
                    if salida.fecha == fecha_hoy and salida.hora and salida.hora < hora_actual:
                        continue
                    tours_con_salidas.setdefault(salida.tour, []).append(salida)
                # Si hay búsqueda activa, el formulario "IR A RESERVAR"
                # solo debe mostrar horarios de ese día buscado.
                tours_reserva_directa = tours_con_salidas
    if request.method == "POST":
        salida_id = request.POST.get("salida_id")
        adultos = _parse_int(request.POST.get("adultos"))
        ninos = _parse_int(request.POST.get("ninos"))
        edades_ninos_raw = request.POST.getlist("edades_ninos")
        nombre = (request.POST.get("nombre") or "").strip()
        apellidos = (request.POST.get("apellidos") or "").strip()
        correo = (request.POST.get("correo") or "").strip().lower()
        telefono = (request.POST.get("telefono") or "").strip()
        identificacion = (request.POST.get("identificacion") or "").strip()

        salida = get_object_or_404(SalidaTour.objects.select_related("tour"), id=salida_id)
        if salida.fecha < fecha_hoy or (salida.fecha == fecha_hoy and salida.hora and salida.hora < hora_actual):
            messages.error(request, "Esa salida ya no esta vigente. Selecciona un turno disponible.")
            return _redir_secretaria_reservar()
        total_personas = adultos + ninos
        if total_personas <= 0:
            messages.error(request, "Debes registrar al menos un pasajero.")
            return _redir_secretaria_reservar()
        if not salida.hay_cupo(adultos, ninos):
            messages.error(request, "No hay cupos disponibles para esa salida.")
            return _redir_secretaria_reservar()
        if not all([nombre, apellidos, telefono, identificacion]):
            messages.error(request, "Completa todos los datos del cliente.")
            return _redir_secretaria_reservar()
        if not correo:
            correo = f"sin-correo-reserva-{timezone.now().strftime('%Y%m%d%H%M%S')}@tortugatur.local"

        edades_ninos = []
        if ninos > 0:
            if len(edades_ninos_raw) != ninos:
                messages.error(request, "Debes ingresar la edad de cada nino.")
                return _redir_secretaria_reservar()
            try:
                edades_ninos = [int(v) for v in edades_ninos_raw]
            except (TypeError, ValueError):
                messages.error(request, "Debes ingresar edades validas para los ninos.")
                return _redir_secretaria_reservar()
            if any(edad < 0 for edad in edades_ninos):
                messages.error(request, "La edad del nino no puede ser negativa.")
                return _redir_secretaria_reservar()

        total_ninos = sum(_precio_nino_por_edad(edad) for edad in edades_ninos)
        total_pagar = (adultos * salida.tour.precio_adulto_final()) + total_ninos
        reserva = Reserva.objects.create(
            usuario=None,
            salida=salida,
            adultos=adultos,
            ninos=ninos,
            total_pagar=total_pagar,
            nombre=nombre,
            apellidos=apellidos,
            correo=correo,
            telefono=telefono,
            identificacion=identificacion,
            estado="pendiente",
            creado_por=request.user,
        )

        messages.success(
            request,
            f"Reserva #{reserva.id:06d} creada. Ahora continua con el cobro (tarjeta o efectivo).",
        )
        checkout_url = reverse("checkout_reserva", args=[reserva.id])
        if embed_mode:
            checkout_url = f"{checkout_url}?embed=1"
        return redirect(checkout_url)

    # Imagen de portada por tour para vista de reserva asistida.
    tour_ids = {t.id for t in todos_los_tours}
    tour_ids.update({t.id for t in tours_con_salidas.keys()})
    tour_ids.update({t.id for t in tours_reserva_directa.keys()})
    portada_map = {}
    if tour_ids:
        galerias = (
            Galeria.objects.filter(tour_id__in=tour_ids)
            .select_related("tour")
            .order_by("tour_id", "-fecha_agregada")
        )
        for g in galerias:
            if g.tour_id not in portada_map:
                portada_map[g.tour_id] = g.obtener_imagen_url()

    for tour in todos_los_tours:
        tour.imagen_portada = portada_map.get(tour.id, "")
    for tour in list(tours_con_salidas.keys()):
        tour.imagen_portada = portada_map.get(tour.id, "")
    for tour in list(tours_reserva_directa.keys()):
        tour.imagen_portada = portada_map.get(tour.id, "")

    return render(
        request,
        "core/panel/secretaria_reservar.html",
        {
            "destinos": destinos,
            "tours_con_salidas": tours_con_salidas,
            "todos_los_tours": todos_los_tours,
            "tours_reserva_directa": tours_reserva_directa,
            "destino_id": destino_id,
            "fecha_busqueda": fecha,
            "fecha_busqueda_display": fecha_display,
            "destino_seleccionado": destino_seleccionado,
            "child_price_0_2": str(CHILD_PRICE_0_2),
            "child_price_3_5": str(CHILD_PRICE_3_5),
            "child_price_normal": str(CHILD_PRICE_NORMAL),
            "fecha_hoy_iso": fecha_hoy.isoformat(),
        },
    )

@require_POST
@login_required
@user_passes_test(es_admin_o_secretaria)
def procesar_pago_efectivo(request, reserva_id):
    _cancelar_reservas_agencia_vencidas()
    embed_mode = (request.POST.get("embed") == "1") or (request.GET.get("embed") == "1")
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if reserva.estado == "pagada":
        messages.warning(request, "La reserva ya está pagada.")
        if embed_mode:
            if es_secretaria(request.user) and not es_admin(request.user):
                return redirect(f"{reverse('secretaria_reservar')}?embed=1")
            return redirect(f"{reverse('admin_reservas')}?embed=1")
        return redirect("checkout_reserva", reserva_id=reserva_id)
    
    try:
        # Generar ticket antes si no existe
        if not hasattr(reserva, 'ticket'):
            Ticket.objects.create(
                reserva=reserva,
                codigo=f"TKT-{reserva.id:06d}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            )
        _mark_reserva_paid(reserva.id, "efectivo", payload={"method": "efectivo", "user": request.user.username})
        messages.success(request, f"¡Reserva #{reserva.id:06d} cobrada en EFECTIVO exitosamente!")
    except ValueError as e:
        messages.error(request, str(e))
    if embed_mode:
        if es_secretaria(request.user) and not es_admin(request.user):
            return redirect(f"{reverse('secretaria_reservar')}?embed=1")
        return redirect(f"{reverse('admin_reservas')}?embed=1")
    return redirect("checkout_reserva", reserva_id=reserva_id)


@require_POST
@login_required
def cancelar_reserva_checkout(request, reserva_id):
    reserva = get_object_or_404(Reserva.objects.select_related("salida"), id=reserva_id)
    embed_mode = (request.POST.get("embed") == "1") or (request.GET.get("embed") == "1")

    # Permisos: admin, secretaria que la creó, o usuario dueño de la reserva.
    es_dueno_turista = bool(reserva.usuario_id and reserva.usuario_id == request.user.id)
    es_dueno_secretaria = bool(reserva.creado_por_id and reserva.creado_por_id == request.user.id)
    if not (es_admin(request.user) or es_dueno_turista or es_dueno_secretaria):
        messages.error(request, "No tienes permiso para cancelar esta reserva.")
        if embed_mode:
            if es_secretaria(request.user) and not es_admin(request.user):
                return redirect(f"{reverse('secretaria_reservar')}?embed=1")
            return redirect(f"{reverse('admin_reservas')}?embed=1")
        return redirect("home")

    if reserva.estado == "pagada":
        messages.error(request, "No se puede cancelar una reserva ya pagada.")
        if embed_mode:
            if es_secretaria(request.user) and not es_admin(request.user):
                return redirect(f"{reverse('secretaria_reservar')}?embed=1")
            return redirect(f"{reverse('admin_reservas')}?embed=1")
        return redirect("mis_reservas")

    if reserva.estado != "cancelada":
        reserva.estado = "cancelada"
        reserva.save(update_fields=["estado"])
        _recalcular_disponibilidad_salida(reserva.salida)

    messages.success(request, f"Reserva #{reserva.id:06d} cancelada correctamente.")
    if embed_mode:
        if es_secretaria(request.user) and not es_admin(request.user):
            return redirect(f"{reverse('secretaria_reservar')}?embed=1")
        return redirect(f"{reverse('admin_reservas')}?embed=1")
    if es_dueno_turista:
        return redirect("mis_reservas")
    return redirect("home")

@login_required
@user_passes_test(es_admin)
def admin_secretarias(request):
    group_secretaria, _ = Group.objects.get_or_create(name=GROUP_SECRETARIA)

    if request.method == "POST":
        first_name = (request.POST.get("first_name") or "").strip()
        last_name = (request.POST.get("last_name") or "").strip()
        email = (request.POST.get("email") or "").strip().lower()
        cedula = _normalizar_cedula(request.POST.get("cedula"))

        if not first_name or not email or not cedula:
            messages.error(request, "Nombres, correo y cédula son obligatorios.")
            return redirect("admin_secretarias")
        if len(cedula) < 6:
            messages.error(request, "La cédula debe tener al menos 6 caracteres.")
            return redirect("admin_secretarias")
        if User.objects.filter(email__iexact=email).exists():
            messages.error(request, "Ese correo ya existe.")
            return redirect("admin_secretarias")

        username_base = _username_secretaria_base(first_name, last_name)
        username = _username_unico(username_base)
        password = cedula

        user = User.objects.create_user(
            username=username,
            password=password,
            first_name=first_name,
            last_name=last_name,
            email=email,
            is_staff=False,
        )
        user.groups.add(group_secretaria)
        perfil, _ = UserProfile.objects.get_or_create(user=user)
        perfil.cedula = cedula
        perfil.force_password_change = True
        perfil.save(update_fields=["cedula", "force_password_change"])
        request.session["admin_last_secretaria_credentials"] = {
            "usuario": username,
            "password": password,
            "correo": email,
            "cedula": cedula,
            "tipo": "nueva",
        }
        messages.success(request, f"Secretaria creada. Usuario: {username} | Clave temporal (cédula): {password}")
        return redirect("admin_secretarias")

    secretarias = group_secretaria.user_set.all().order_by("username")
    credenciales_generadas = request.session.pop("admin_last_secretaria_credentials", None)
    return render(
        request,
        "core/panel/secretarias.html",
        {
            "secretarias": secretarias,
            "credenciales_generadas": credenciales_generadas,
        },
    )


@login_required
@user_passes_test(es_admin)
def toggle_secretaria_estado(request, user_id):
    if request.method != "POST":
        return redirect("admin_secretarias")

    group_secretaria = Group.objects.filter(name=GROUP_SECRETARIA).first()
    secretaria = get_object_or_404(User, id=user_id)
    if not group_secretaria or not secretaria.groups.filter(id=group_secretaria.id).exists():
        messages.error(request, "El usuario seleccionado no pertenece al rol secretaria.")
        return redirect("admin_secretarias")

    secretaria.is_active = not secretaria.is_active
    secretaria.save(update_fields=["is_active"])
    estado = "activada" if secretaria.is_active else "desactivada"
    messages.success(request, f"Cuenta de '{secretaria.username}' {estado} correctamente.")
    return redirect("admin_secretarias")


@login_required
@user_passes_test(es_admin)
def eliminar_secretaria(request, user_id):
    if request.method != "POST":
        return redirect("admin_secretarias")

    group_secretaria = Group.objects.filter(name=GROUP_SECRETARIA).first()
    secretaria = get_object_or_404(User, id=user_id)
    if not group_secretaria or not secretaria.groups.filter(id=group_secretaria.id).exists():
        messages.error(request, "El usuario seleccionado no pertenece al rol secretaria.")
        return redirect("admin_secretarias")

    secretaria.delete()
    messages.success(request, f"Secretaria '{secretaria.username}' eliminada definitivamente.")
    return redirect("admin_secretarias")


@login_required
@user_passes_test(es_admin)
def reset_secretaria_password(request, user_id):
    if request.method != "POST":
        return redirect("admin_secretarias")

    group_secretaria = Group.objects.filter(name=GROUP_SECRETARIA).first()
    secretaria = get_object_or_404(User, id=user_id)
    if not group_secretaria or not secretaria.groups.filter(id=group_secretaria.id).exists():
        messages.error(request, "El usuario seleccionado no pertenece al rol secretaria.")
        return redirect("admin_secretarias")

    perfil, _ = UserProfile.objects.get_or_create(user=secretaria)
    if not perfil.cedula:
        messages.error(request, "Esta secretaria no tiene cédula registrada. Actualízala antes de resetear.")
        return redirect("admin_secretarias")

    new_password = perfil.cedula
    secretaria.set_password(new_password)
    secretaria.save(update_fields=["password"])
    perfil.force_password_change = True
    perfil.save(update_fields=["force_password_change"])
    request.session["admin_last_secretaria_credentials"] = {
        "usuario": secretaria.username,
        "password": new_password,
        "correo": secretaria.email or "",
        "cedula": perfil.cedula,
        "tipo": "reset",
    }
    messages.success(request, f"Contraseña temporal (cédula) para '{secretaria.username}': {new_password}")
    return redirect("admin_secretarias")


@login_required
@user_passes_test(es_admin)
def reset_agencia_password(request, user_id):
    embed_mode = (request.GET.get("embed") == "1") or (request.POST.get("embed") == "1")

    def _redir_agencias(to_top=False):
        url = reverse("admin_agencias")
        if embed_mode:
            url = f"{url}?embed=1"
        if to_top:
            url = f"{url}#credenciales-generadas"
        return redirect(url)

    if request.method != "POST":
        return _redir_agencias(to_top=True)

    group_agencia = Group.objects.filter(name=GROUP_AGENCIA).first()
    agencia = get_object_or_404(User, id=user_id)
    es_agencia_user = bool(group_agencia and agencia.groups.filter(id=group_agencia.id).exists())
    es_agencia_perfil = bool(getattr(getattr(agencia, "perfil", None), "is_agencia", False))
    if not (es_agencia_user or es_agencia_perfil):
        messages.error(request, "El usuario seleccionado no pertenece al rol agencia.")
        return _redir_agencias(to_top=True)

    perfil, _ = UserProfile.objects.get_or_create(user=agencia)
    if not perfil.cedula:
        messages.error(request, "Esta agencia no tiene cédula registrada. Actualízala antes de resetear.")
        return _redir_agencias(to_top=True)

    new_password = perfil.cedula
    agencia.set_password(new_password)
    agencia.save(update_fields=["password"])
    perfil.force_password_change = True
    perfil.save(update_fields=["force_password_change"])
    request.session["admin_last_agencia_credentials"] = {
        "usuario": agencia.username,
        "password": new_password,
        "correo": agencia.email or "",
        "cedula": perfil.cedula,
        "tipo": "reset",
    }
    messages.success(request, f"Contraseña temporal (cédula) para agencia '{agencia.username}': {new_password}")
    return _redir_agencias(to_top=True)

