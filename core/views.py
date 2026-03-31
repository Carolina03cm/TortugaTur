import json
import logging
import hmac
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import requests
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import login, logout
from django.contrib.auth.models import Group, User
from django.core.mail import send_mail, EmailMessage, EmailMultiAlternatives
from django.core.management import call_command
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from datetime import timedelta, datetime, time, date
from django.http import JsonResponse, HttpResponse
from django.urls import reverse, NoReverseMatch
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache
from django.db import transaction
from django.db.models import Q, Sum, Count, Max
from django.core.paginator import Paginator
from collections import defaultdict
import re
import unicodedata
from urllib.parse import urlencode
from .models import Destino, Tour, SiteVisit, SalidaTour, Reserva, Pago, Resena, Ticket, EmpresaConfig, Galeria, UserProfile
from .utils import generar_ticket_pdf, generar_actividad_dia_pdf, generar_factura_agencia_mensual_pdf
from .forms import DestinoForm, TourForm, RegistroTuristaForm, ContactoForm, TuristaLoginForm, EmpresaConfigForm

logger = logging.getLogger(__name__)

CHILD_PRICE_0_2 = Decimal("10.00")
CHILD_PRICE_3_5 = Decimal("35.00")
CHILD_PRICE_NORMAL = Decimal("70.00")
GROUP_SECRETARIA = "secretaria"
GROUP_AGENCIA = "agencia"
ESTADOS_AGENCIA_VISIBLES = [
    "solicitud_agencia",
    "cotizada_agencia",
    "confirmada_agencia",
    "pagada_parcial_agencia",
    "pagada_total_agencia",
    "rechazada_agencia",
    "bloqueada_por_agencia",
]
ESTADOS_AGENCIA_ACTIVOS = [
    "solicitud_agencia",
    "cotizada_agencia",
    "confirmada_agencia",
    "pagada_parcial_agencia",
    "bloqueada_por_agencia",
]
ESTADO_COTIZACION_PENDIENTE = "cotizacion_pendiente"
PHONE_COUNTRY_CODES = [
    ("593", "Ecuador (+593)"),
    ("57", "Colombia (+57)"),
    ("51", "Peru (+51)"),
    ("56", "Chile (+56)"),
    ("54", "Argentina (+54)"),
    ("52", "Mexico (+52)"),
    ("1", "Estados Unidos (+1)"),
    ("34", "Espana (+34)"),
]


def _filtrar_tours_para_usuario(qs, user):
    if user and user.is_authenticated and es_agencia(user):
        return qs.filter(visible_para_agencias=True)
    return qs


def _aplica_descuento_ninos(tour, user=None):
    if user and user.is_authenticated and es_agencia(user):
        return bool(getattr(tour, "descuento_ninos_agencia_activo", False))
    return bool(getattr(tour, "descuento_ninos_activo", True))


def _precio_nino_por_edad(edad_nino, tour=None, user=None):
    if tour is not None and not _aplica_descuento_ninos(tour, user):
        return tour.precio_adulto_final()
    if edad_nino is None:
        return CHILD_PRICE_NORMAL
    base_price = None
    if tour is not None:
        base_price = tour.precio_nino_final()
    if not base_price or base_price <= 0:
        base_price = CHILD_PRICE_NORMAL
    ratio_0_2 = (CHILD_PRICE_0_2 / CHILD_PRICE_NORMAL)
    ratio_3_5 = (CHILD_PRICE_3_5 / CHILD_PRICE_NORMAL)
    if edad_nino <= 2:
        return (base_price * ratio_0_2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if edad_nino <= 5:
        return (base_price * ratio_3_5).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return base_price


def _texto_a_items(texto):
    if not texto:
        return []
    return [linea.strip() for linea in str(texto).splitlines() if linea.strip()]


def _es_reserva_interna(reserva):
    return bool(reserva and getattr(reserva, "estado", "") == ESTADO_COTIZACION_PENDIENTE)


def _telefono_para_whatsapp(telefono):
    digits = re.sub(r"\D+", "", telefono or "")
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("593") and len(digits) >= 11:
        return digits
    # Normalizacion por defecto para numeros locales de Ecuador.
    if len(digits) == 10 and digits.startswith("0"):
        return f"593{digits[1:]}"
    if len(digits) == 9:
        return f"593{digits}"
    return digits if len(digits) >= 8 else ""


def _telefono_desde_form(post_data, field_name="telefono", code_field="telefono_codigo", number_field="telefono_numero"):
    telefono_directo = (post_data.get(field_name) or "").strip()
    if telefono_directo:
        return telefono_directo

    codigo = re.sub(r"\D+", "", (post_data.get(code_field) or "").strip())
    numero = re.sub(r"\D+", "", (post_data.get(number_field) or "").strip())
    if not numero:
        return ""
    if codigo and numero.startswith("0"):
        numero = numero[1:]
    return f"+{codigo}{numero}" if codigo else numero


def _telefono_normalizado_desde_form(post_data, field_name="telefono", code_field="telefono_codigo", number_field="telefono_numero"):
    telefono = _telefono_desde_form(
        post_data,
        field_name=field_name,
        code_field=code_field,
        number_field=number_field,
    )
    telefono_whatsapp = _telefono_para_whatsapp(telefono)
    if telefono_whatsapp:
        return f"+{telefono_whatsapp}"
    return (telefono or "").strip()


def _whatsapp_reserva_interna_url(reserva):
    telefono = _telefono_para_whatsapp(getattr(reserva, "telefono", ""))
    if not telefono:
        return ""
    mensaje = (
        f"Hola {reserva.nombre or ''}, tu cotizacion para el tour "
        f"{reserva.salida.tour.nombre} ya esta lista. "
        f"El valor asignado a pagar es de ${reserva.total_pagar} USD."
    ).strip()
    return f"https://wa.me/{telefono}?{urlencode({'text': mensaje})}"


def _calcular_limite_pago_agencia(fecha_reserva):
    """
    Regla de recordatorio mensual para agencias:
    - Recordatorio 7 dias antes de terminar el mes de la reserva.
    - No existe un limite por salida ni penalizacion por fecha/hora.
    """
    if isinstance(fecha_reserva, datetime):
        fecha_ref = fecha_reserva.date()
    else:
        fecha_ref = fecha_reserva

    if not fecha_ref:
        fecha_ref = timezone.localdate()

    if fecha_ref.month == 12:
        first_next_month = date(fecha_ref.year + 1, 1, 1)
    else:
        first_next_month = date(fecha_ref.year, fecha_ref.month + 1, 1)
    last_day = first_next_month - timedelta(days=1)
    recordatorio_date = last_day - timedelta(days=7)

    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(recordatorio_date, time(23, 59, 59)), tz)


def _agenda_actividad(reservas, salidas):
    agenda = defaultdict(list)

    for reserva in reservas:
        if reserva.estado == "cancelada":
            continue
        fecha_reserva_local = timezone.localtime(reserva.fecha_reserva)
        fecha_ref = fecha_reserva_local.date()
        agenda[fecha_ref].append({
            "tipo": "reserva",
            "dt": fecha_reserva_local,
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
        fecha_reserva_local = timezone.localtime(res.fecha_reserva)
        items.append({
            "tipo": "reserva",
            "dt": fecha_reserva_local,
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
    tours_destacados = _filtrar_tours_para_usuario(Tour.objects.all(), request.user)[:3]
    fotos_galeria_home = list(Galeria.objects.order_by("-fecha_agregada")[:3])
    for foto in fotos_galeria_home:
        foto.imagen_home_url = foto.obtener_imagen_url()
    currency_code, currency_rate = _currency_context(request)
    visitas_home_total = SiteVisit.objects.count()
    for tour in tours_destacados:
        display = _tour_price_display(tour, currency_rate, request.user)
        tour.precio_adulto_display = display["adulto"]
        tour.precio_nino_display = display["nino"]

    if request.GET.get('pago') == 'ok':
        from django.contrib import messages
        messages.success(request, "¡Gracias! Tu pago está siendo procesado. El estado de tu reserva se actualizará en unos minutos una vez confirmado.")

    context = {
        "destinos": destinos,
        "tours_destacados": tours_destacados,
        "fotos_galeria_home": fotos_galeria_home,
        "visitas_home_total": visitas_home_total,
        "currency_code": currency_code,
        "currency_options": list(getattr(settings, "CURRENCY_RATES", {}).keys()),
    }

    return render(request, "core/home.html", context)

def tours(request):
    tours = _filtrar_tours_para_usuario(Tour.objects.select_related("destino").all(), request.user)
    destinos = Destino.objects.all()
    currency_code, currency_rate = _currency_context(request)
    for tour in tours:
        display = _tour_price_display(tour, currency_rate, request.user)
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
    if es_agencia(request.user):
        salidas_brutas = salidas_brutas.filter(tour__visible_para_agencias=True)

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
        display = _tour_price_display(tour, currency_rate, request.user)
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
    if es_agencia(request.user) and not tour.visible_para_agencias:
        messages.error(request, "Este tour no esta habilitado para agencias.")
        return redirect("tours")
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
            telefono = _telefono_normalizado_desde_form(request.POST)
            identificacion = request.POST.get("identificacion", "")
            edades_ninos = []
            aplica_descuento_ninos = _aplica_descuento_ninos(tour, request.user)

            if ninos > 0 and aplica_descuento_ninos:
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
            charter_agencia = (request.POST.get("charter_agencia") == "on")
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
                if False and turno_ocupado_agencia:
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

            # Validar datos obligatorios solo para flujo normal (no agencias).
            if request.user.is_authenticated and (not usuario_es_agencia) and not all([nombre, telefono, identificacion]):
                error_msg = "Completa todos tus datos personales."
                if is_ajax:
                    return JsonResponse({'error': error_msg}, status=400)
                messages.error(request, error_msg)
                return redirect('tour_detalle', pk=pk)

            if usuario_es_agencia:
                if ninos > 0:
                    error_msg = "Las agencias solo pueden registrar pasajeros adultos o usar charter."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                if charter_agencia:
                    adultos = 16
                    ninos = 0
                    edades_ninos = []
                    total_personas = 16

                if total_personas > 16:
                    error_msg = "Las agencias solo pueden bloquear un máximo de 16 pasajeros por reserva."
                    if is_ajax:
                        return JsonResponse({'error': error_msg}, status=400)
                    messages.error(request, error_msg)
                    return redirect('tour_detalle', pk=pk)

                codigo_agencia = request.POST.get("codigo_agencia", "")
                archivo_agencia = request.FILES.get("archivo_agencia")

                perfil_agencia = getattr(request.user, "perfil", None)
                nombre_reserva = (
                    (nombre or "").strip()
                    or request.user.get_full_name().strip()
                    or request.user.username
                )
                telefono_reserva = (
                    (telefono or "").strip()
                    or (getattr(perfil_agencia, "telefono", "") or "").strip()
                    or "N/A"
                )
                identificacion_reserva = (
                    (identificacion or "").strip()
                    or (getattr(perfil_agencia, "cedula", "") or "").strip()
                    or request.user.username
                )

                # Crear solicitud de bloqueo. Secretaria la acepta/rechaza.
                with transaction.atomic():
                    salida = SalidaTour.objects.select_for_update().get(id=salida.id)
                    if salida.cupos_disponibles < total_personas:
                         raise ValueError("Cupos no disponibles")

                    reserva = Reserva.objects.create(
                        usuario=request.user,
                        salida=salida,
                        adultos=adultos,
                        ninos=ninos,
                        total_pagar=Decimal("0.00"),
                        tipo_reserva="agencia",
                        nombre=nombre_reserva,
                        apellidos=request.user.last_name or "",
                        correo=request.user.email,
                        telefono=telefono_reserva,
                        identificacion=identificacion_reserva,
                        estado="solicitud_agencia",
                        codigo_agencia=codigo_agencia,
                        archivo_agencia=archivo_agencia,
                        hora_turno_agencia=hora_turno_agencia,
                        hora_turno_libre=hora_turno_libre,
                        agencia_nombre=request.user.first_name or request.user.username,
                        agencia_contacto=nombre_reserva,
                        agencia_telefono=telefono_reserva,
                        agencia_correo=(request.user.email or "").strip().lower(),
                    )

                _notificar_secretarias_solicitud_agencia(reserva)

                msg = "Solicitud enviada. Secretaria recibio la notificacion y debe aceptar o rechazar el bloqueo."
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
                if tour.ocultar_precio:
                    total_pagar = Decimal("0.00")
                else:
                    precio_adulto = tour.precio_adulto_final()
                    if aplica_descuento_ninos:
                        total_ninos = sum(_precio_nino_por_edad(edad, tour=tour, user=request.user) for edad in edades_ninos)
                    else:
                        total_ninos = ninos * precio_adulto
                    total_pagar = (adultos * precio_adulto) + total_ninos

                # Crear la reserva con estado PENDIENTE (hasta que pague)
                reserva = Reserva.objects.create(
                    usuario=request.user if request.user.is_authenticated else None,
                    salida=salida,
                    adultos=adultos,
                    ninos=ninos,
                    total_pagar=total_pagar,
                    tipo_reserva="general",
                    nombre=nombre if nombre else (request.user.first_name if request.user.is_authenticated else ""),
                    apellidos="",  # Puedes agregar este campo al formulario si quieres
                    correo=request.user.email if request.user.is_authenticated else "",
                    telefono=telefono,
                    identificacion=identificacion,
                    estado=ESTADO_COTIZACION_PENDIENTE if tour.ocultar_precio else "pendiente"
                )

                # NO descontamos cupos aquÃ­, se descontarÃ¡n despuÃ©s del pago

                if tour.ocultar_precio:
                    msg = "Reserva registrada. El equipo asignara el valor y luego podras pagarla desde Mis Reservas."
                    if is_ajax:
                        return JsonResponse({
                            'success': True,
                            'reserva_id': reserva.id,
                            'redirect_url': reverse('mis_reservas')
                        })
                    messages.success(request, msg)
                    return redirect('mis_reservas')

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
    price_display = _tour_price_display(tour, currency_rate, request.user)
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
        "child_price_0_2": str(_precio_nino_por_edad(0, tour=tour, user=request.user)),
        "child_price_3_5": str(_precio_nino_por_edad(4, tour=tour, user=request.user)),
        "child_price_normal": str(_precio_nino_por_edad(8, tour=tour, user=request.user)),
        "aplica_descuento_ninos": _aplica_descuento_ninos(tour, request.user),
        "tour_oculta_precio": tour.ocultar_precio,
        "tour_incluye_items": _texto_a_items(tour.incluye),
        "tour_no_incluye_items": _texto_a_items(tour.no_incluye),
        "tour_recomendaciones_items": _texto_a_items(tour.recomendaciones),
        "tour_info_importante_items": _texto_a_items(tour.informacion_importante),
        "phone_country_codes": PHONE_COUNTRY_CODES,
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
    es_agencia_ticket = _es_reserva_agencia(reserva)
    monto_ticket = reserva.total_pagar
    if es_agencia_ticket and (reserva.monto_pagado_agencia or Decimal("0.00")) > 0:
        monto_ticket = reserva.monto_pagado_agencia
    return render(
        request,
        "core/ticket.html",
        {
            "reserva": reserva,
            "empresa": _empresa_config(),
            "monto_ticket": monto_ticket,
            "es_agencia_ticket": es_agencia_ticket,
        },
    )

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


def _post_pago_redirect_for_user(user, embed_mode=False):
    if user and user.is_authenticated:
        if es_secretaria(user) and not es_admin(user):
            try:
                base = reverse("panel_secretaria")
            except NoReverseMatch:
                base = "/panel/secretaria/"
        else:
            base = reverse("panel_admin")
    else:
        base = reverse("home")
    if embed_mode and base.startswith("/panel/"):
        return f"{base}?embed=1"
    return base


def _panel_secretaria_url():
    try:
        return reverse("panel_secretaria")
    except NoReverseMatch:
        return "/panel/secretaria/"


def _redir_admin_reservas(request, tipo_default="general"):
    tipo = (request.POST.get("tipo") or request.GET.get("tipo") or tipo_default or "general").strip().lower()
    if tipo not in ["general", "agencia"]:
        tipo = tipo_default if tipo_default in ["general", "agencia"] else "general"

    params = [("tipo", tipo)]
    fecha = (request.POST.get("fecha") or request.GET.get("fecha") or "").strip()
    if fecha:
        params.append(("fecha", fecha))

    if tipo == "agencia":
        estado_agencia = (request.POST.get("estado_agencia") or request.GET.get("estado_agencia") or "").strip().lower()
        if estado_agencia in ["activos", "solicitud_agencia", "bloqueada_por_agencia", "pagada_total_agencia"]:
            params.append(("estado_agencia", estado_agencia))

    base = reverse("admin_reservas")
    return redirect(f"{base}?{urlencode(params)}")

@login_required
@user_passes_test(es_admin_o_secretaria)
def panel_admin(request):
    _cancelar_reservas_agencia_vencidas()
    # En dashboard principal siempre mostramos actividad/KPIs del dia actual.
    # El admin puede consultar otras fechas desde los modulos de actividad y filtros.
    actividad_fecha = timezone.localdate()

    solicitudes_agencia_pendientes = Reserva.objects.filter(estado="solicitud_agencia").count()
    context = {
        "es_secretaria_panel": es_secretaria(request.user) and not es_admin(request.user),
        "actividad_fecha": actividad_fecha,
        "panel_rol_label": "Administrador" if es_admin(request.user) else "Secretaria",
        "panel_profile_url": "",
        "panel_workspace_enabled": True,
        "solicitudes_agencia_pendientes": solicitudes_agencia_pendientes,
    }
    perfil_user = getattr(request.user, "perfil", None)
    if perfil_user and getattr(perfil_user, "foto", None):
        try:
            context["panel_profile_url"] = perfil_user.foto.url
        except Exception:
            context["panel_profile_url"] = ""
    ahora = timezone.now()
    hoy = timezone.localdate()
    inicio_mes = hoy.replace(day=1)
    context["recordatorios_agencia"] = (
        Reserva.objects.filter(
            fecha_reserva__date__gte=inicio_mes,
        )
        .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
        .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
        .select_related("salida__tour", "usuario")
        .order_by("agencia_nombre", "fecha_reserva")[:12]
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
        estado__in=["bloqueada_por_agencia", "cotizada_agencia", "confirmada_agencia", "pagada_parcial_agencia"],
        fecha_reserva__date=actividad_fecha,
    )
    solicitudes_agencia_hoy = Reserva.objects.filter(
        estado="solicitud_agencia",
        fecha_reserva__date=actividad_fecha,
    )
    penalizaciones_hoy = Pago.objects.filter(
        estado__in=["created", "approved"],
        payload__tipo="penalizacion_incumplimiento",
        creado_en__date=actividad_fecha,
    )
    alertas_operativas = []
    if solicitudes_agencia_pendientes:
        alertas_operativas.append({
            "titulo": "Solicitudes de agencia pendientes",
            "detalle": f"{solicitudes_agencia_pendientes} solicitud(es) esperan aprobacion de secretaria.",
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
        "solicitudes_agencia_hoy": solicitudes_agencia_hoy.count(),
        "penalizaciones_hoy": penalizaciones_hoy.count(),
    }
    context["alertas_operativas"] = alertas_operativas

    # Notificaciones de agencia visibles para admin y secretaria (campanita superior).
    bloqueos_qs_global = (
        Reserva.objects.filter(estado="solicitud_agencia")
        .select_related("salida__tour")
        .order_by("-fecha_reserva")[:12]
    )
    enlace_agencia = f"{reverse('admin_reservas')}?tipo=agencia"
    context["notificaciones_bloqueos"] = [
        {
            "notif_id": f"bloqueo-{r.id}-{r.estado}",
            "referencia": f"#{r.id:05d}",
            "detalle": f"{r.salida.tour.nombre} - {r.nombre} {r.apellidos}".strip(),
            "enlace": enlace_agencia,
        }
        for r in bloqueos_qs_global
    ]
    context["notificaciones_secretarias"] = []
    if request.user.is_staff or request.user.is_superuser:
        fecha_desde_raw = (request.GET.get("fecha_desde") or "").strip()
        fecha_hasta_raw = (request.GET.get("fecha_hasta") or "").strip()
        tipo_actividad = (request.GET.get("tipo_actividad") or "todos").strip().lower()
        secretaria_id = (request.GET.get("secretaria") or "").strip()

        try:
            fecha_desde = datetime.strptime(fecha_desde_raw, "%Y-%m-%d").date() if fecha_desde_raw else (hoy - timedelta(days=7))
        except ValueError:
            fecha_desde = hoy - timedelta(days=7)
        try:
            fecha_hasta = datetime.strptime(fecha_hasta_raw, "%Y-%m-%d").date() if fecha_hasta_raw else hoy
        except ValueError:
            fecha_hasta = hoy
        if fecha_desde > fecha_hasta:
            fecha_desde, fecha_hasta = fecha_hasta, fecha_desde

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

        # KPI estilo SaaS con variacion vs ayer.
        ayer = hoy - timedelta(days=1)
        reservas_ayer = Reserva.objects.filter(fecha_reserva__date=ayer).exclude(estado="cancelada").count()
        ingresos_ayer = (
            Pago.objects.filter(estado="paid", creado_en__date=ayer).aggregate(total=Sum("monto")).get("total")
            or Decimal("0.00")
        )
        tours_programados_hoy = (
            SalidaTour.objects.filter(fecha=hoy).values("tour_id").distinct().count()
        )
        tours_programados_ayer = (
            SalidaTour.objects.filter(fecha=ayer).values("tour_id").distinct().count()
        )
        salidas_activas_hoy = SalidaTour.objects.filter(fecha__gte=hoy, cupos_disponibles__gt=0).count()
        salidas_activas_ayer = SalidaTour.objects.filter(fecha__gte=ayer, cupos_disponibles__gt=0).count()

        def _delta(actual, previo):
            actual_d = Decimal(actual or 0)
            previo_d = Decimal(previo or 0)
            if previo_d == 0:
                if actual_d == 0:
                    return "0%"
                return "+100%"
            cambio = ((actual_d - previo_d) / previo_d) * Decimal("100")
            pref = "+" if cambio >= 0 else ""
            return f"{pref}{cambio.quantize(Decimal('0.1'))}%"

        context["dashboard_kpis"] = [
            {
                "titulo": "Reservas hoy",
                "valor": reservas_hoy.count(),
                "delta": _delta(reservas_hoy.count(), reservas_ayer),
                "icono": "event_available",
                "tono": "emerald",
            },
            {
                "titulo": "Ingresos hoy",
                "valor": f"${(pagos_hoy.aggregate(total=Sum('monto')).get('total') or Decimal('0.00')).quantize(Decimal('0.01'))}",
                "delta": _delta(pagos_hoy.aggregate(total=Sum("monto")).get("total") or Decimal("0.00"), ingresos_ayer),
                "icono": "payments",
                "tono": "teal",
            },
            {
                "titulo": "Tours programados",
                "valor": tours_programados_hoy,
                "delta": _delta(tours_programados_hoy, tours_programados_ayer),
                "icono": "map",
                "tono": "lime",
            },
            {
                "titulo": "Salidas activas",
                "valor": salidas_activas_hoy,
                "delta": _delta(salidas_activas_hoy, salidas_activas_ayer),
                "icono": "directions_boat",
                "tono": "slate",
            },
        ]

        # Centro de actividad con filtros.
        secretaria_group = Group.objects.filter(name__iexact=GROUP_SECRETARIA).first()
        secretarias = list(secretaria_group.user_set.filter(is_active=True).order_by("username")) if secretaria_group else []
        actividad_rows = []
        reservas_actividad = (
            Reserva.objects.filter(fecha_reserva__date__range=[fecha_desde, fecha_hasta])
            .select_related("salida__tour", "creado_por", "gestionada_por")
            .order_by("-fecha_reserva")
        )
        if secretaria_id:
            reservas_actividad = reservas_actividad.filter(creado_por_id=secretaria_id)
        reservas_actividad = reservas_actividad[:400]

        for r in reservas_actividad:
            if tipo_actividad not in ["todos", "reserva", "bloqueo", "cancelacion"]:
                pass
            tipo = "bloqueo" if r.estado in ESTADOS_AGENCIA_VISIBLES else "reserva"
            if r.estado == "cancelada":
                tipo = "cancelacion"
            if tipo_actividad != "todos" and tipo_actividad != tipo:
                continue
            secretaria_nombre = (
                r.gestionada_por.username
                if (tipo == "bloqueo" and r.gestionada_por)
                else (r.creado_por.username if r.creado_por else "Sistema")
            )
            actividad_rows.append({
                "tipo": tipo,
                "referencia": f"#{r.id:05d}",
                "secretaria": secretaria_nombre,
                "detalle": f"{r.salida.tour.nombre} - {r.nombre} {r.apellidos}".strip(),
                "estado": r.estado,
                "hora": r.fecha_reserva,
                "monto": r.total_pagar,
            })

        pagos_actividad = (
            Pago.objects.filter(estado="paid", creado_en__date__range=[fecha_desde, fecha_hasta])
            .select_related("reserva__salida__tour", "reserva__creado_por")
            .order_by("-creado_en")
        )
        if secretaria_id:
            pagos_actividad = pagos_actividad.filter(reserva__creado_por_id=secretaria_id)
        pagos_actividad = pagos_actividad[:300]
        if tipo_actividad in ["todos", "pago"]:
            for p in pagos_actividad:
                actividad_rows.append({
                    "tipo": "pago",
                    "referencia": f"PG-{p.id:05d}",
                    "secretaria": (p.reserva.creado_por.username if p.reserva and p.reserva.creado_por else "Sistema"),
                    "detalle": f"Pago {p.get_proveedor_display()} - Reserva #{p.reserva_id:05d}",
                    "estado": "confirmado",
                    "hora": p.creado_en,
                    "monto": p.monto,
                })

        actividad_rows = sorted(actividad_rows, key=lambda x: x["hora"], reverse=True)[:120]
        context["actividad_rows"] = actividad_rows
        context["filtros_actividad"] = {
            "fecha_desde": fecha_desde.isoformat(),
            "fecha_hasta": fecha_hasta.isoformat(),
            "tipo_actividad": tipo_actividad,
            "secretaria": secretaria_id,
        }
        context["secretarias_filtro"] = secretarias

        # Reservas del dia.
        context["reservas_dia_rows"] = list(
            Reserva.objects.filter(fecha_reserva__date=hoy)
            .exclude(estado="cancelada")
            .select_related("salida__tour")
            .order_by("-fecha_reserva")[:25]
        )

        # Graficos de operacion.
        desde_30 = hoy - timedelta(days=29)
        reservas_30 = (
            Reserva.objects.filter(fecha_reserva__date__range=[desde_30, hoy])
            .exclude(estado="cancelada")
            .values("fecha_reserva__date")
            .annotate(total=Count("id"))
        )
        ingresos_30 = (
            Pago.objects.filter(estado="paid", creado_en__date__range=[desde_30, hoy])
            .values("creado_en__date")
            .annotate(total=Sum("monto"))
        )
        map_res = {x["fecha_reserva__date"]: int(x["total"] or 0) for x in reservas_30}
        map_ing = {x["creado_en__date"]: float(x["total"] or 0) for x in ingresos_30}
        labels_30 = []
        data_res_30 = []
        data_ing_30 = []
        for i in range(30):
            f = desde_30 + timedelta(days=i)
            labels_30.append(f.strftime("%d/%m"))
            data_res_30.append(map_res.get(f, 0))
            data_ing_30.append(map_ing.get(f, 0))
        tours_top = (
            Reserva.objects.filter(estado="pagada", fecha_reserva__date__range=[desde_30, hoy])
            .values("salida__tour__nombre")
            .annotate(total=Count("id"))
            .order_by("-total")[:6]
        )
        ocupacion_tours = (
            SalidaTour.objects.filter(fecha__gte=hoy)
            .select_related("tour")
            .order_by("fecha", "hora")[:8]
        )
        ocupacion_labels = []
        ocupacion_values = []
        for salida in ocupacion_tours:
            capacidad = max(int(salida.cupo_maximo or 0), 1)
            ocupadas = max(capacidad - int(salida.cupos_disponibles or 0), 0)
            porcentaje = round((ocupadas / capacidad) * 100, 1)
            ocupacion_labels.append(f"{salida.tour.nombre} {salida.fecha.strftime('%d/%m')}")
            ocupacion_values.append(porcentaje)
        context["dashboard_charts"] = {
            "labels_30": labels_30,
            "reservas_30": data_res_30,
            "ingresos_30": data_ing_30,
            "tours_top_labels": [t["salida__tour__nombre"] for t in tours_top],
            "tours_top_values": [int(t["total"] or 0) for t in tours_top],
            "ocupacion_labels": ocupacion_labels,
            "ocupacion_values": ocupacion_values,
        }
        context["dashboard_charts_json"] = json.dumps(context["dashboard_charts"])

        # Notificaciones operativas.
        notificaciones = []
        nuevas_reservas = Reserva.objects.filter(fecha_reserva__date=hoy).exclude(estado="cancelada").order_by("-fecha_reserva")[:6]
        for r in nuevas_reservas:
            notificaciones.append({
                "titulo": "Nueva reserva",
                "detalle": f"#{r.id:05d} {r.salida.tour.nombre}",
                "nivel": "info",
                "notif_id": f"reserva-nueva-{r.id}",
                "enlace": reverse("admin_reservas"),
                "dt": r.fecha_reserva,
            })
        cambios_reserva = Reserva.objects.filter(estado__in=["cancelada"] + ESTADOS_AGENCIA_VISIBLES).order_by("-fecha_reserva")[:8]
        for r in cambios_reserva:
            notificaciones.append({
                "titulo": "Cambio en reserva",
                "detalle": f"#{r.id:05d} estado {r.estado}",
                "nivel": "warning" if r.estado != "cancelada" else "danger",
                "notif_id": f"reserva-cambio-{r.id}-{r.estado}",
                "enlace": reverse("admin_reservas"),
                "dt": r.fecha_reserva,
            })
        tours_por_llenarse = SalidaTour.objects.filter(fecha__gte=hoy, cupos_disponibles__lte=2).select_related("tour").order_by("fecha", "hora")[:6]
        for s in tours_por_llenarse:
            notificaciones.append({
                "titulo": "Tour por llenarse",
                "detalle": f"{s.tour.nombre} {s.fecha.strftime('%d/%m')} ({s.cupos_disponibles} cupos)",
                "nivel": "warning",
                "notif_id": f"salida-lleno-{s.id}",
                "enlace": reverse("admin_salidas"),
                "dt": timezone.make_aware(datetime.combine(s.fecha, s.hora or time.min), timezone.get_current_timezone()),
            })
        for idx, a in enumerate(alertas_operativas[:5], start=1):
            notificaciones.append({
                "titulo": a["titulo"],
                "detalle": a["detalle"],
                "nivel": "danger" if a.get("nivel") == "alto" else "info",
                "notif_id": f"alerta-{idx}-{(a['titulo'] or '').lower().replace(' ', '-')}",
                "enlace": a.get("enlace") or reverse("panel_actividad"),
                "dt": timezone.now(),
            })
        context["notificaciones_dashboard"] = sorted(notificaciones, key=lambda x: x["dt"], reverse=True)[:12]

        bloqueos_qs = (
            Reserva.objects.filter(estado__in=ESTADOS_AGENCIA_VISIBLES)
            .select_related("salida__tour")
            .order_by("-fecha_reserva")[:6]
        )
        context["notificaciones_bloqueos"] = [
            {
                "notif_id": f"bloqueo-{r.id}-{r.estado}",
                "referencia": f"#{r.id:05d}",
                "detalle": f"{r.salida.tour.nombre} - {r.nombre} {r.apellidos}".strip(),
                "enlace": reverse("admin_reservas"),
            }
            for r in bloqueos_qs
        ]

        actividad_secretarias_qs = (
            Reserva.objects.filter(creado_por__isnull=False)
            .exclude(estado="cancelada")
            .select_related("creado_por", "gestionada_por")
            .order_by("-fecha_reserva")[:8]
        )
        context["notificaciones_secretarias"] = [
            {
                "notif_id": f"secretaria-{r.id}-{r.estado}",
                "secretaria": (
                    r.gestionada_por.username
                    if (r.estado in ESTADOS_AGENCIA_VISIBLES and r.gestionada_por)
                    else (r.creado_por.username if r.creado_por else "Sistema")
                ),
                "tipo": "bloqueo" if r.estado in ESTADOS_AGENCIA_VISIBLES else "reserva",
                "referencia": f"#{r.id:05d}",
                "enlace": reverse("admin_reservas"),
            }
            for r in actividad_secretarias_qs
            if r.creado_por and r.creado_por.username
        ]

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
                estado__in=["bloqueada_por_agencia", "cotizada_agencia", "confirmada_agencia", "pagada_parcial_agencia"],
                fecha_reserva__date=actividad_fecha,
            ).count(),
            "solicitudes_agencia_hoy": Reserva.objects.filter(
                estado="solicitud_agencia",
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
        notificaciones_agencia = list(
            Reserva.objects.filter(estado__in=ESTADOS_AGENCIA_VISIBLES)
            .select_related("salida__tour", "usuario")
            .order_by("-fecha_reserva")[:20]
        )
        prioridad_estado = {"solicitud_agencia": 0, "cotizada_agencia": 1, "confirmada_agencia": 2, "pagada_parcial_agencia": 3, "bloqueada_por_agencia": 4}
        notificaciones_agencia.sort(
            key=lambda r: (
                prioridad_estado.get(r.estado, 9),
                r.salida.fecha if r.salida else timezone.localdate(),
                r.hora_turno_agencia or time.max,
            )
        )
        for item in notificaciones_agencia:
            if item.estado == "solicitud_agencia":
                item.notif_titulo = "Nueva solicitud de bloqueo"
                item.notif_nivel = "alta"
                item.notif_estado = "Pendiente de aprobación"
            elif item.estado == "cotizada_agencia":
                item.notif_titulo = "Reserva cotizada"
                item.notif_nivel = "media"
                item.notif_estado = "Pendiente de confirmacion de agencia"
            elif item.estado == "confirmada_agencia":
                item.notif_titulo = "Reserva confirmada"
                item.notif_nivel = "media"
                item.notif_estado = "Pendiente de pago"
            else:
                item.notif_titulo = "Bloqueo de agencia confirmado"
                item.notif_nivel = "media"
                item.notif_estado = "Pendiente de registro de pago"
        context["notificaciones_agencia_secretaria"] = notificaciones_agencia[:10]
        context["notificaciones_agencia_total"] = len(notificaciones_agencia)

        # Campanita secretaria: mostrar solo nuevas solicitudes de agencia.
        solicitudes_campana = [r for r in notificaciones_agencia if r.estado == "solicitud_agencia"][:12]
        context["notificaciones_bloqueos"] = [
            {
                "notif_id": f"bloqueo-{r.id}-{r.estado}",
                "referencia": f"#{r.id:05d}",
                "detalle": f"{r.salida.tour.nombre} - {r.nombre} {r.apellidos}".strip(),
                "enlace": f"{reverse('admin_reservas')}?tipo=agencia",
            }
            for r in solicitudes_campana
        ]
        context["notificaciones_secretarias"] = []
        context["notificaciones_dashboard"] = [
            {
                "notif_id": item["notif_id"],
                "titulo": "Nueva solicitud de agencia",
                "detalle": item["detalle"],
                "enlace": item["enlace"],
                "dt": timezone.now(),
            }
            for item in context["notificaciones_bloqueos"]
        ]

    # Unificamos admin y secretaria en el mismo dashboard embebido para navegar
    # dentro del panel sin saltar a vistas externas.
    return render(request, "core/panel/dashboard_admin.html", context)


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
            .select_related("salida__tour", "creado_por", "gestionada_por", "usuario")
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
                    (res.gestionada_por.username if (res.estado in ESTADOS_AGENCIA_VISIBLES and res.gestionada_por) else None)
                    or (res.creado_por.username if res.creado_por else (res.usuario.username if res.usuario else "web"))
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
@user_passes_test(es_admin_o_secretaria)
def admin_reservas(request):
    _cancelar_reservas_agencia_vencidas()
    for reserva_estado in Reserva.objects.exclude(estado__in=["pagada", "pagada_total_agencia"]).prefetch_related("pagos"):
        tiene_pago_reserva = any(
            p.estado == "paid" and (p.payload or {}).get("tipo") != "penalizacion_incumplimiento"
            for p in reserva_estado.pagos.all()
        )
        if tiene_pago_reserva:
            reserva_estado.estado = "pagada_total_agencia" if _es_reserva_agencia(reserva_estado) else "pagada"
            reserva_estado.save(update_fields=["estado"])

    fecha_filtro = request.GET.get('fecha')
    tipo_actual = (request.GET.get("tipo") or "general").strip().lower()
    estado_agencia = (request.GET.get("estado_agencia") or "activos").strip().lower()
    hoy = timezone.localdate()
    if tipo_actual not in ["general", "agencia"]:
        tipo_actual = "general"
    if estado_agencia not in ["activos", "solicitud_agencia", "bloqueada_por_agencia", "pagada_total_agencia"]:
        estado_agencia = "activos"

    reservas_query = (
        Reserva.objects.select_related("salida__tour")
        .prefetch_related("pagos")
        .exclude(estado="pendiente")
        .exclude(estado__in=["cancelada"])
    )

    if fecha_filtro:
        reservas_query = reservas_query.filter(salida__fecha=fecha_filtro)

    reservas_agencia_base_qs = reservas_query.filter(
        Q(tipo_reserva="agencia")
        | Q(estado__in=ESTADOS_AGENCIA_VISIBLES)
        | Q(codigo_agencia__isnull=False)
    ).distinct()
    reservas_agencia_qs = reservas_agencia_base_qs
    if estado_agencia == "activos":
        reservas_agencia_qs = reservas_agencia_qs.filter(
            estado__in=["solicitud_agencia", "bloqueada_por_agencia", "pagada_total_agencia", "pagada"]
        )
    else:
        reservas_agencia_qs = reservas_agencia_qs.filter(estado=estado_agencia)
    reservas_generales_qs = reservas_query.exclude(id__in=reservas_agencia_base_qs.values("id"))

    reservas = reservas_generales_qs if tipo_actual == "general" else reservas_agencia_qs
    reservas = reservas.order_by("-id")

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

        fecha_local = timezone.localtime(reserva.fecha_reserva).date()
        reserva.dias_sin_pago = max((hoy - fecha_local).days, 0)

        if reserva.estado == "solicitud_agencia":
            reserva.estado_mostrar = "pendiente"
        elif reserva.estado == ESTADO_COTIZACION_PENDIENTE:
            reserva.estado_mostrar = "cotizacion lista" if (reserva.total_pagar or Decimal("0.00")) > 0 else "pendiente cotizacion"
        elif reserva.estado == "cotizada_agencia":
            reserva.estado_mostrar = "cotizada"
        elif reserva.estado == "confirmada_agencia":
            reserva.estado_mostrar = "confirmada"
        elif reserva.estado == "pagada_parcial_agencia":
            reserva.estado_mostrar = "pagada parcial"
        elif reserva.estado in ["pagada_total_agencia", "pagada"] or reserva.tiene_pago:
            reserva.estado_mostrar = "pagada total"
        elif reserva.estado == "rechazada_agencia":
            reserva.estado_mostrar = "rechazada"
        elif reserva.estado == "bloqueada_por_agencia":
            reserva.estado_mostrar = "bloqueada agencia"
        else:
            reserva.estado_mostrar = reserva.estado
        reserva.whatsapp_url_cliente = _whatsapp_reserva_interna_url(reserva) if _es_reserva_interna(reserva) and (reserva.total_pagar or Decimal("0.00")) > 0 else ""
        reserva.whatsapp_cliente_disponible = bool(reserva.whatsapp_url_cliente)

    hoy = timezone.localdate()
    kpi_agencia = {
        "solicitudes_pendientes": Reserva.objects.filter(estado="solicitud_agencia").count(),
        "bloqueadas_sin_pago": Reserva.objects.filter(estado="bloqueada_por_agencia").count(),
        "pagadas_hoy": Reserva.objects.filter(
            tipo_reserva="agencia",
            estado__in=["pagada_total_agencia", "pagada"],
            fecha_reserva__date=hoy,
        ).count(),
        "vencen_hoy": Reserva.objects.filter(
            estado="bloqueada_por_agencia",
            limite_pago_agencia__date=hoy,
        ).count(),
    }

    return render(
        request,
        "core/panel/reservas.html",
        {
            "reservas": reservas,
            "tipo_actual": tipo_actual,
            "estado_agencia": estado_agencia,
            "totales_tab": {
                "general": reservas_generales_qs.count(),
                "agencia": reservas_agencia_base_qs.count(),
            },
            "kpi_agencia": kpi_agencia,
            "resumen_financiero_reservas": {
                "ingresos_total": ingresos_total,
                "ingresos_mes": ingresos_mes,
                "ingresos_anio": ingresos_anio,
                "ingresos_fecha_filtro": ingresos_fecha_filtro,
            },
        },
    )


@login_required
@user_passes_test(es_admin_o_secretaria)
def admin_agencias_sin_pago(request):
    hoy = timezone.localdate()
    limite_7d = hoy - timedelta(days=7)
    reservas_query = (
        Reserva.objects.select_related("salida__tour", "usuario")
        .prefetch_related("pagos")
        .exclude(estado="pendiente")
    )

    base_agencia = (
        reservas_query.filter(
            Q(tipo_reserva="agencia")
            | Q(estado__in=ESTADOS_AGENCIA_VISIBLES)
            | Q(codigo_agencia__isnull=False)
        )
        .exclude(estado__in=["cancelada", "rechazada_agencia"])
        .exclude(pagos__estado="paid")
    )

    reservas_agencia = (
        base_agencia.filter(fecha_reserva__date__gt=limite_7d)
        .distinct()
        .order_by("-fecha_reserva")
    )

    pendientes_envio_qs = (
        base_agencia.filter(fecha_reserva__date__gt=limite_7d)
        .filter(alerta_7d_agencia_enviada_en__isnull=True)
        .order_by("agencia_correo", "fecha_reserva")
        .distinct()
    )

    if request.method == "POST" and request.POST.get("accion") == "enviar_uno":
        reserva_id = request.POST.get("reserva_id")
        if not reserva_id:
            messages.error(request, "Reserva invalida.")
            return redirect("admin_agencias_sin_pago")

        reserva = (
            base_agencia.filter(id=reserva_id)
            .filter(fecha_reserva__date__gt=limite_7d)
            .first()
        )
        if not reserva:
            messages.error(request, "Reserva no disponible para envio.")
            return redirect("admin_agencias_sin_pago")

        email = (reserva.agencia_correo or "").strip().lower()
        if not email and reserva.usuario and reserva.usuario.email:
            email = (reserva.usuario.email or "").strip().lower()
        if not email:
            messages.error(request, "La reserva no tiene correo de agencia.")
            return redirect("admin_agencias_sin_pago")

        try:
            fecha_label = timezone.localtime(reserva.fecha_reserva).strftime("%d/%m/%Y")
            tour_nombre = reserva.salida.tour.nombre if reserva.salida and reserva.salida.tour else "Tour"
            subject = f"Pago pendiente: reserva sin pago ({hoy.strftime('%d/%m/%Y')})"
            body = (
                "Hola,\n\n"
                "Esta reserva de agencia aun no registra pago:\n"
                f"- Reserva #{reserva.id:06d} | {tour_nombre} | {fecha_label} | Total: ${reserva.total_pagar}\n\n"
                f"Deuda total: ${reserva.total_pagar}\n\n"
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

            reserva.alerta_7d_agencia_enviada_en = timezone.now()
            reserva.save(update_fields=["alerta_7d_agencia_enviada_en"])
            messages.success(request, f"Correo enviado a {email}.")
        except Exception as e:
            messages.error(request, f"No se pudo enviar el correo: {e}")
        return redirect("admin_agencias_sin_pago")

    if request.method == "POST" and request.POST.get("accion") == "enviar_recordatorios":
        pendientes_envio = list(pendientes_envio_qs)

        agrupadas = {}
        for reserva in pendientes_envio:
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
                logger.error("Fallo enviando recordatorio manual a %s: %s", email, e)

        messages.success(request, f"Se enviaron {enviados} recordatorio(s).")
        return redirect("admin_agencias_sin_pago")

    for reserva in reservas_agencia:
        fecha_local = timezone.localtime(reserva.fecha_reserva).date()
        reserva.dias_sin_pago = max((hoy - fecha_local).days, 0)
        reserva.tiene_pago = any(pago.estado == "paid" for pago in reserva.pagos.all())

        if reserva.estado == "solicitud_agencia":
            reserva.estado_mostrar = "pendiente"
        elif reserva.estado == ESTADO_COTIZACION_PENDIENTE:
            reserva.estado_mostrar = "cotizacion lista" if (reserva.total_pagar or Decimal("0.00")) > 0 else "pendiente cotizacion"
        elif reserva.estado == "cotizada_agencia":
            reserva.estado_mostrar = "cotizada"
        elif reserva.estado == "confirmada_agencia":
            reserva.estado_mostrar = "confirmada"
        elif reserva.estado == "pagada_parcial_agencia":
            reserva.estado_mostrar = "pagada parcial"
        elif reserva.estado in ["pagada_total_agencia", "pagada"] or reserva.tiene_pago:
            reserva.estado_mostrar = "pagada total"
        elif reserva.estado == "rechazada_agencia":
            reserva.estado_mostrar = "rechazada"
        elif reserva.estado == "bloqueada_por_agencia":
            reserva.estado_mostrar = "bloqueada agencia"
        else:
            reserva.estado_mostrar = reserva.estado
        reserva.whatsapp_url_cliente = _whatsapp_reserva_interna_url(reserva) if _es_reserva_interna(reserva) and (reserva.total_pagar or Decimal("0.00")) > 0 else ""
        reserva.whatsapp_cliente_disponible = bool(reserva.whatsapp_url_cliente)

    return render(
        request,
        "core/panel/agencias_sin_pago.html",
        {
            "reservas": reservas_agencia,
            "pendientes_envio_count": pendientes_envio_qs.count(),
        },
    )


@login_required
@user_passes_test(es_admin_o_secretaria)
def admin_reservas_estado_json(request):
    tipo_actual = (request.GET.get("tipo") or "general").strip().lower()
    if tipo_actual != "agencia":
        return JsonResponse({"items": []})

    reservas_query = (
        Reserva.objects.select_related("salida__tour")
        .prefetch_related("pagos")
        .exclude(estado__in=["pendiente", "cancelada"])
    )
    reservas_agencia = reservas_query.filter(
        Q(tipo_reserva="agencia")
        | Q(estado__in=ESTADOS_AGENCIA_VISIBLES)
        | Q(codigo_agencia__isnull=False)
    ).distinct()

    items = []
    for reserva in reservas_agencia:
        tiene_pago = any(pago.estado == "paid" for pago in reserva.pagos.all())

        if reserva.estado == "solicitud_agencia":
            estado_mostrar = "pendiente"
        elif reserva.estado == "cotizada_agencia":
            estado_mostrar = "cotizada"
        elif reserva.estado == "confirmada_agencia":
            estado_mostrar = "confirmada"
        elif reserva.estado == "pagada_parcial_agencia":
            estado_mostrar = "pagada parcial"
        elif reserva.estado in ["pagada_total_agencia", "pagada"] or tiene_pago:
            estado_mostrar = "pagada total"
        elif reserva.estado == "rechazada_agencia":
            estado_mostrar = "rechazada"
        elif reserva.estado == "bloqueada_por_agencia":
            estado_mostrar = "bloqueada agencia"
        else:
            estado_mostrar = reserva.estado

        items.append({
            "id": reserva.id,
            "estado": reserva.estado,
            "estado_mostrar": estado_mostrar,
            "tiene_pago": tiene_pago,
        })

    return JsonResponse({"items": items})

@login_required
@user_passes_test(es_admin)
def cambiar_estado_reserva(request, reserva_id):
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if request.method == "POST":
        if _es_reserva_agencia(reserva):
            messages.error(request, "Las reservas de agencia no se pueden modificar manualmente desde admin.")
            return redirect("admin_reservas")
        nuevo_estado = request.POST.get("estado")
        if nuevo_estado in ["pendiente", ESTADO_COTIZACION_PENDIENTE, "confirmada", "cancelada", "pagada"]:
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
@require_POST
def gestionar_solicitud_agencia(request, reserva_id):
    reserva = get_object_or_404(Reserva.objects.select_related("salida__tour"), id=reserva_id)
    if not _es_reserva_agencia(reserva):
        messages.error(request, "La reserva seleccionada no corresponde a una agencia.")
        return _redir_admin_reservas(request, "agencia")
    accion = (request.POST.get("accion") or "").strip().lower()
    if accion not in ["aceptar", "rechazar"]:
        messages.error(request, "Accion invalida.")
        return _redir_admin_reservas(request, "agencia")

    if accion == "rechazar":
        if reserva.estado != "solicitud_agencia":
            messages.warning(request, "Solo puedes rechazar solicitudes pendientes.")
            return _redir_admin_reservas(request, "agencia")
        reserva.estado = "cancelada"
        reserva.gestionada_por = request.user
        reserva.save(update_fields=["estado", "gestionada_por"])
        _recalcular_disponibilidad_salida(reserva.salida)
        messages.success(request, f"Solicitud de agencia #{reserva.id:06d} rechazada.")
        return _redir_admin_reservas(request, "agencia")

    with transaction.atomic():
        reserva = Reserva.objects.select_for_update().select_related("salida").get(id=reserva.id)
        salida = SalidaTour.objects.select_for_update().get(id=reserva.salida_id)
        if reserva.estado != "solicitud_agencia":
            messages.warning(request, "La solicitud ya fue procesada por otro usuario.")
            return _redir_admin_reservas(request, "agencia")

        total_personas = reserva.adultos + reserva.ninos
        if total_personas > 16:
            messages.error(request, "Las solicitudes de agencia no pueden superar 16 pasajeros.")
            return _redir_admin_reservas(request, "agencia")
        if salida.cupos_disponibles < total_personas:
            messages.error(request, "No hay cupos disponibles para aceptar este bloqueo.")
            return _redir_admin_reservas(request, "agencia")

        reserva.estado = "bloqueada_por_agencia"
        reserva.tipo_reserva = "agencia"
        reserva.limite_pago_agencia = _calcular_limite_pago_agencia(reserva.fecha_reserva)
        reserva.gestionada_por = request.user
        reserva.save(update_fields=["estado", "tipo_reserva", "limite_pago_agencia", "gestionada_por"])
        salida.cupos_disponibles = 0
        salida.save(update_fields=["cupos_disponibles"])

    messages.success(request, f"Solicitud de agencia #{reserva.id:06d} aceptada y bloqueada.")
    return _redir_admin_reservas(request, "agencia")


@login_required
@user_passes_test(es_admin_o_secretaria)
@require_POST
def registrar_pago_agencia(request, reserva_id):
    reserva = get_object_or_404(Reserva.objects.select_related("salida"), id=reserva_id)
    if not _es_reserva_agencia(reserva):
        messages.error(request, "La reserva seleccionada no corresponde a una agencia.")
        return _redir_admin_reservas(request, "agencia")
    if reserva.estado != "bloqueada_por_agencia":
        messages.error(request, "Solo puedes registrar pago en reservas de agencia bloqueadas.")
        return _redir_admin_reservas(request, "agencia")
    if not reserva.total_pagar or reserva.total_pagar <= 0:
        messages.error(request, "Primero registra el monto pendiente antes de confirmar el pago.")
        return _redir_admin_reservas(request, "agencia")

    monto_pagado = _parse_decimal(request.POST.get("monto_pagado"))
    if monto_pagado is None:
        monto_pagado = reserva.total_pagar
    if monto_pagado is None or monto_pagado <= 0:
        messages.error(request, "Ingresa un monto pagado valido mayor a 0.")
        return _redir_admin_reservas(request, "agencia")

    reserva.total_pagar = monto_pagado
    reserva.monto_pagado_agencia = monto_pagado
    reserva.tipo_reserva = "agencia"
    reserva.gestionada_por = request.user
    reserva.save(update_fields=["total_pagar", "monto_pagado_agencia", "tipo_reserva", "gestionada_por"])
    try:
        _mark_reserva_paid(
            reserva.id,
            "efectivo",
            payload={
                "tipo": "pago_agencia_manual",
                "registrado_por": request.user.username,
                "monto_registrado": str(monto_pagado),
            },
        )
    except ValueError as e:
        messages.error(request, str(e))
        return _redir_admin_reservas(request, "agencia")

    messages.success(request, f"Pago de agencia registrado en reserva #{reserva.id:06d} por ${monto_pagado}.")
    return _redir_admin_reservas(request, "agencia")


@login_required
@user_passes_test(es_admin_o_secretaria)
@require_POST
def registrar_monto_agencia(request, reserva_id):
    reserva = get_object_or_404(Reserva.objects.select_related("salida"), id=reserva_id)
    if not _es_reserva_agencia(reserva):
        messages.error(request, "La reserva seleccionada no corresponde a una agencia.")
        return _redir_admin_reservas(request, "agencia")
    if reserva.estado != "bloqueada_por_agencia":
        messages.error(request, "Solo puedes registrar monto en reservas de agencia bloqueadas.")
        return _redir_admin_reservas(request, "agencia")

    monto = _parse_decimal(request.POST.get("monto_pagado"))
    if monto is None or monto <= 0:
        messages.error(request, "Ingresa un monto valido mayor a 0.")
        return _redir_admin_reservas(request, "agencia")

    reserva.total_pagar = monto
    reserva.monto_pagado_agencia = Decimal("0.00")
    reserva.tipo_reserva = "agencia"
    reserva.gestionada_por = request.user
    if not reserva.limite_pago_agencia:
        reserva.limite_pago_agencia = _calcular_limite_pago_agencia(reserva.fecha_reserva)
    reserva.save(update_fields=["total_pagar", "monto_pagado_agencia", "tipo_reserva", "gestionada_por", "limite_pago_agencia"])

    messages.success(request, f"Monto registrado en reserva #{reserva.id:06d} por ${monto}.")
    return _redir_admin_reservas(request, "agencia")


@login_required
@user_passes_test(es_admin_o_secretaria)
@require_POST
def registrar_monto_reserva_interna(request, reserva_id):
    reserva = get_object_or_404(Reserva.objects.select_related("salida__tour"), id=reserva_id)
    if _es_reserva_agencia(reserva):
        messages.error(request, "La reserva seleccionada corresponde a una agencia.")
        return _redir_admin_reservas(request, "general")
    if reserva.estado != ESTADO_COTIZACION_PENDIENTE:
        messages.error(request, "Solo puedes asignar monto a reservas internas pendientes de cotizacion.")
        return _redir_admin_reservas(request, "general")
    if (reserva.total_pagar or Decimal("0.00")) > 0:
        messages.warning(request, "Esta reserva ya tiene un monto asignado y no se puede modificar.")
        return _redir_admin_reservas(request, "general")

    monto = _parse_decimal(request.POST.get("monto_pagado"))
    if monto is None or monto <= 0:
        messages.error(request, "Ingresa un monto valido mayor a 0.")
        return _redir_admin_reservas(request, "general")

    envio_correo = False
    reserva.total_pagar = monto
    reserva.gestionada_por = request.user
    reserva.save(update_fields=["total_pagar", "gestionada_por"])
    envio_correo = _send_internal_quote_ready_email(reserva)
    messages.success(
        request,
        f"Monto asignado en reserva #{reserva.id:06d} por ${monto}. "
        f"{'Se envio correo al cliente.' if envio_correo else 'El cliente ya puede pagar desde Mis Reservas.'}",
    )
    return _redir_admin_reservas(request, "general")


@login_required
@user_passes_test(es_admin_o_secretaria)
@require_POST
def actualizar_telefono_reserva_interna(request, reserva_id):
    reserva = get_object_or_404(Reserva.objects.select_related("salida__tour"), id=reserva_id)
    if _es_reserva_agencia(reserva):
        messages.error(request, "La reserva seleccionada corresponde a una agencia.")
        return _redir_admin_reservas(request, "general")
    if not _es_reserva_interna(reserva):
        messages.error(request, "Solo puedes actualizar telefono en reservas internas.")
        return _redir_admin_reservas(request, "general")

    telefono = _telefono_normalizado_desde_form(request.POST)
    if not _telefono_para_whatsapp(telefono):
        messages.error(request, "Ingresa un telefono valido para WhatsApp.")
        return _redir_admin_reservas(request, "general")

    reserva.telefono = telefono
    reserva.gestionada_por = request.user
    reserva.save(update_fields=["telefono", "gestionada_por"])
    messages.success(request, f"Telefono actualizado en reserva #{reserva.id:06d}.")
    return _redir_admin_reservas(request, "general")

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
    es_secretaria_user = es_secretaria(request.user) and not es_admin(request.user)
    
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
            if es_secretaria_user:
                if not hora_post:
                    messages.error(request, "Debes ingresar la hora de inicio del tour.")
                    return redirect("crear_salida")
                horas.append(hora_post)
            else:
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

    return render(request, "core/panel/crear_salida.html", {"tours": tours, "es_secretaria": es_secretaria_user})

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
    """Maneja el inicio de sesion y la redireccion al tour original."""
    if request.user.is_authenticated:
        if request.user.is_staff or request.user.is_superuser:
            return redirect("panel_admin")
        if es_secretaria(request.user):
            return redirect(_panel_secretaria_url())
        return redirect("home")

    next_url = request.GET.get('next', 'home')

    if request.method == 'POST':
        form = TuristaLoginForm(data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            if not (user.is_staff or user.is_superuser or es_secretaria(user)):
                messages.success(request, f"Que bueno verte de nuevo, {user.first_name}!")
            if user.is_staff or user.is_superuser:
                request.session["welcome_admin"] = True
                return redirect("panel_admin")
            if es_secretaria(user):
                request.session["welcome_secretaria"] = True
                return redirect(_panel_secretaria_url())
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
    # Limpia cualquier mensaje pendiente (por ejemplo, de login) para evitar dobles.
    storage = messages.get_messages(request)
    for _ in storage:
        pass
    logout(request)
    messages.info(request, "Has cerrado sesión correctamente.")
    return redirect('home')

from django.contrib.auth.decorators import login_required

@login_required
def panel_inicio(request):
    if es_secretaria(request.user) and not es_admin(request.user):
        return redirect(_panel_secretaria_url())
    if not (request.user.is_staff or request.user.is_superuser):
        return redirect("home")

    hoy = timezone.localdate()
    fecha_desde_raw = (request.GET.get("fecha_desde") or "").strip()
    fecha_hasta_raw = (request.GET.get("fecha_hasta") or "").strip()
    estado_filtro = (request.GET.get("estado") or "todos").strip().lower()
    estados_validos = {"todos", "pendiente", "cotizacion_pendiente", "pagada", "cancelada", "solicitud_agencia", "bloqueada_por_agencia"}
    if estado_filtro not in estados_validos:
        estado_filtro = "todos"

    try:
        fecha_desde = datetime.strptime(fecha_desde_raw, "%Y-%m-%d").date() if fecha_desde_raw else (hoy - timedelta(days=29))
    except ValueError:
        fecha_desde = hoy - timedelta(days=29)
    try:
        fecha_hasta = datetime.strptime(fecha_hasta_raw, "%Y-%m-%d").date() if fecha_hasta_raw else hoy
    except ValueError:
        fecha_hasta = hoy
    if fecha_desde > fecha_hasta:
        fecha_desde, fecha_hasta = fecha_hasta, fecha_desde
    if (fecha_hasta - fecha_desde).days > 62:
        fecha_desde = fecha_hasta - timedelta(days=62)

    pendientes_agencia = Reserva.objects.filter(estado="solicitud_agencia").count()
    pendientes_cotizacion_interna = Reserva.objects.filter(estado=ESTADO_COTIZACION_PENDIENTE).count()
    salidas_con_pocos_cupos = SalidaTour.objects.filter(fecha__gte=hoy, cupos_disponibles__lte=3).count()
    actividad_base = (
        Reserva.objects.select_related("salida__tour")
        .filter(fecha_reserva__date__range=[fecha_desde, fecha_hasta])
        .order_by("-fecha_reserva")
    )
    if estado_filtro != "todos":
        actividad_base = actividad_base.filter(estado=estado_filtro)
    nuevas_reservas = actividad_base[:12]

    reservas_chart_qs = Reserva.objects.filter(fecha_reserva__date__range=[fecha_desde, fecha_hasta])
    if estado_filtro != "todos":
        reservas_chart_qs = reservas_chart_qs.filter(estado=estado_filtro)
    else:
        reservas_chart_qs = reservas_chart_qs.exclude(estado="cancelada")
    reservas_chart = reservas_chart_qs.values("fecha_reserva__date").annotate(total=Count("id"))
    reservas_map = {r["fecha_reserva__date"]: int(r["total"] or 0) for r in reservas_chart}

    pagos_chart = (
        Pago.objects.filter(estado="paid", creado_en__date__range=[fecha_desde, fecha_hasta])
        .values("creado_en__date")
        .annotate(total=Sum("monto"))
    )
    pagos_map = {p["creado_en__date"]: float(p["total"] or 0) for p in pagos_chart}
    labels = []
    data_reservas = []
    data_ingresos = []
    dias = (fecha_hasta - fecha_desde).days + 1
    for i in range(max(dias, 1)):
        d = fecha_desde + timedelta(days=i)
        labels.append(d.strftime("%d/%m"))
        data_reservas.append(reservas_map.get(d, 0))
        data_ingresos.append(pagos_map.get(d, 0))

    notificaciones = []
    for r in nuevas_reservas[:5]:
        notificaciones.append({
            "notif_id": f"reserva-{r.id}-{r.estado}",
            "titulo": f"Reserva #{r.id:05d}",
            "detalle": (r.salida.tour.nombre if r.salida and r.salida.tour else "Reserva nueva"),
            "fecha": r.fecha_reserva,
            "url": f"{reverse('admin_reservas')}?tipo=general",
        })
    if pendientes_agencia:
        notificaciones.insert(0, {
            "notif_id": f"agencia-pendiente-{pendientes_agencia}",
            "titulo": "Solicitudes de agencia",
            "detalle": f"{pendientes_agencia} pendientes por revisar",
            "fecha": timezone.now(),
            "url": f"{reverse('admin_reservas')}?tipo=agencia",
        })
    if pendientes_cotizacion_interna:
        notificaciones.insert(0, {
            "notif_id": f"cotizacion-interna-{pendientes_cotizacion_interna}",
            "titulo": "Reservas internas",
            "detalle": f"{pendientes_cotizacion_interna} pendiente(s) de cotizar",
            "fecha": timezone.now(),
            "url": f"{reverse('admin_reservas')}?tipo=general",
        })

    limite_7d = hoy - timedelta(days=7)
    pendientes_envio_count = (
        Reserva.objects.filter(fecha_reserva__date__gt=limite_7d)
        .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
        .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
        .distinct()
        .count()
    )
    if pendientes_envio_count:
        notificaciones.insert(0, {
            "notif_id": f"agencia-recordatorios-{pendientes_envio_count}",
            "titulo": "Recordatorios pendientes",
            "detalle": f"{pendientes_envio_count} reserva(s) sin pago por avisar",
            "fecha": timezone.now(),
            "url": f"{reverse('admin_agencias_sin_pago')}",
        })

    inicio_mes = hoy.replace(day=1)
    inicio_anio = hoy.replace(month=1, day=1)
    ingresos_total = Pago.objects.filter(estado="paid").aggregate(total=Sum("monto")).get("total") or Decimal("0.00")
    ingresos_mes = (
        Pago.objects.filter(estado="paid", creado_en__date__gte=inicio_mes)
        .aggregate(total=Sum("monto"))
        .get("total")
        or Decimal("0.00")
    )

    reservas_mes_qs = Reserva.objects.filter(fecha_reserva__date__gte=inicio_mes).exclude(estado="cancelada")
    reservas_anio_qs = Reserva.objects.filter(fecha_reserva__date__gte=inicio_anio).exclude(estado="cancelada")
    inv_mes = reservas_mes_qs.aggregate(adultos=Sum("adultos"), ninos=Sum("ninos"))
    inv_anio = reservas_anio_qs.aggregate(adultos=Sum("adultos"), ninos=Sum("ninos"))

    recordatorios_agencia = (
        Reserva.objects.filter(
            fecha_reserva__date__gte=inicio_mes,
        )
        .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
        .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
        .select_related("salida__tour")
        .order_by("agencia_nombre", "fecha_reserva")[:6]
    )

    actividad_secretarias = []
    for r in nuevas_reservas[:6]:
        actividad_secretarias.append({
            "tipo": "reserva creada" if r.estado not in ESTADOS_AGENCIA_VISIBLES else "bloqueo aplicado",
            "detalle": f"{r.salida.tour.nombre if r.salida and r.salida.tour else 'Tour'} - #{r.id:05d}",
            "hora": timezone.localtime(r.fecha_reserva),
        })

    tour_top_row = (
        Reserva.objects.exclude(estado="cancelada")
        .values("salida__tour__nombre")
        .annotate(total=Count("id"), pasajeros=Sum("adultos") + Sum("ninos"))
        .order_by("-total")
        .first()
    )
    tour_mas_vendido = {
        "nombre": (tour_top_row or {}).get("salida__tour__nombre") or "Sin datos",
        "reservas": int((tour_top_row or {}).get("total") or 0),
        "pasajeros": int((tour_top_row or {}).get("pasajeros") or 0),
    }

    cliente_top_row = (
        Reserva.objects.filter(tipo_reserva="general")
        .exclude(estado="cancelada")
        .values("nombre", "apellidos")
        .annotate(total=Count("id"))
        .order_by("-total")
        .first()
    )
    cliente_fiel = {
        "nombre": (
            f"{(cliente_top_row or {}).get('nombre', '')} {(cliente_top_row or {}).get('apellidos', '')}".strip()
            or "Sin datos"
        ),
        "reservas": int((cliente_top_row or {}).get("total") or 0),
    }

    agencia_top_row = (
        Reserva.objects.filter(tipo_reserva="agencia")
        .exclude(estado__in=["cancelada", "rechazada_agencia"])
        .exclude(agencia_nombre="")
        .values("agencia_nombre")
        .annotate(total=Count("id"))
        .order_by("-total")
        .first()
    )
    agencia_fiel = {
        "nombre": (agencia_top_row or {}).get("agencia_nombre") or "Sin datos",
        "reservas": int((agencia_top_row or {}).get("total") or 0),
    }

    context = {
        "admin_nombre": request.user.get_full_name() or request.user.username,
        "rol": "Administrador",
        "show_welcome": bool(request.session.pop("welcome_admin", False)),
        "kpi_usuarios": User.objects.filter(is_active=True).count(),
        "kpi_tours": Tour.objects.count(),
        "kpi_reservas_hoy": Reserva.objects.filter(fecha_reserva__date=hoy).count(),
        "kpi_ingresos_hoy": Pago.objects.filter(estado="paid", creado_en__date=hoy).aggregate(total=Sum("monto")).get("total") or Decimal("0.00"),
        "kpi_pendientes_agencia": pendientes_agencia,
        "kpi_pendientes_cotizacion": pendientes_cotizacion_interna,
        "kpi_bloqueos_agencia": Reserva.objects.filter(estado="bloqueada_por_agencia").count(),
        "kpi_salidas_alerta": salidas_con_pocos_cupos,
        "kpi_penalizaciones_hoy": Pago.objects.filter(
            estado__in=["created", "approved"],
            payload__tipo="penalizacion_incumplimiento",
            creado_en__date=hoy,
        ).count(),
        "notificaciones": notificaciones[:6],
        "actividad_reciente": nuevas_reservas[:6],
        "resumen_financiero": {
            "ingresos_total": ingresos_total,
            "ingresos_mes": ingresos_mes,
            "reservas_mes": reservas_mes_qs.count(),
            "reservas_anio": reservas_anio_qs.count(),
            "pasajeros_mes": int(inv_mes.get("adultos") or 0) + int(inv_mes.get("ninos") or 0),
            "pasajeros_anio": int(inv_anio.get("adultos") or 0) + int(inv_anio.get("ninos") or 0),
        },
        "recordatorios_agencia": recordatorios_agencia,
        "actividad_secretarias": actividad_secretarias,
        "tour_mas_vendido": tour_mas_vendido,
        "cliente_fiel": cliente_fiel,
        "agencia_fiel": agencia_fiel,
        "filtros": {
            "fecha_desde": fecha_desde.isoformat(),
            "fecha_hasta": fecha_hasta.isoformat(),
            "estado": estado_filtro,
        },
        "dashboard_charts_json": json.dumps({
            "labels": labels,
            "reservas": data_reservas,
            "ingresos": data_ingresos,
        }),
    }
    return render(request, "core/panel/index.html", context)


@login_required
@user_passes_test(es_staff_o_secretaria)
def panel_secretaria(request):
    if es_admin(request.user):
        return redirect("panel_admin")

    _cancelar_reservas_agencia_vencidas()
    hoy = timezone.localdate()
    inicio_mes = hoy.replace(day=1)

    reservas_hoy_qs = (
        Reserva.objects.filter(creado_por=request.user, fecha_reserva__date=hoy)
        .exclude(estado="cancelada")
        .select_related("salida__tour")
    )
    reservas_mes_qs = (
        Reserva.objects.filter(creado_por=request.user, fecha_reserva__date__gte=inicio_mes)
        .exclude(estado="cancelada")
    )
    reservas_agencia_hoy_qs = reservas_hoy_qs.filter(
        Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES)
    )
    pagos_hoy = (
        Pago.objects.filter(
            estado="paid",
            reserva__creado_por=request.user,
            creado_en__date=hoy,
        ).aggregate(total=Sum("monto")).get("total")
        or Decimal("0.00")
    )
    salidas_hoy = SalidaTour.objects.filter(fecha=hoy).count()
    salidas_proximas = (
        SalidaTour.objects.filter(fecha__gte=hoy)
        .select_related("tour")
        .order_by("fecha", "hora")[:8]
    )

    actividad_hoy = _secretaria_actividad_dia(request.user, hoy)[:10]
    bloqueos_agencia_qs = (
        Reserva.objects.filter(estado="solicitud_agencia")
        .select_related("salida__tour")
        .order_by("-fecha_reserva")
    )
    solicitudes_agencia_pendientes = bloqueos_agencia_qs.count()
    notificaciones_bloqueos = []
    for r in bloqueos_agencia_qs[:8]:
        notificaciones_bloqueos.append({
            "notif_id": f"agencia-bloqueo-{r.id}",
            "titulo": f"Bloqueo agencia #{r.id:05d}",
            "detalle": (r.salida.tour.nombre if r.salida and r.salida.tour else "Solicitud de bloqueo"),
            "fecha": timezone.localtime(r.fecha_reserva),
            "url": f"{reverse('admin_reservas')}?tipo=agencia&estado_agencia=solicitud_agencia",
        })

    limite_7d = hoy - timedelta(days=7)
    pendientes_envio_qs = (
        Reserva.objects.filter(fecha_reserva__date__gt=limite_7d)
        .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
        .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
        .distinct()
    )
    pendientes_envio_count = pendientes_envio_qs.count()
    if pendientes_envio_count:
        notificaciones_bloqueos.insert(0, {
            "notif_id": f"agencia-recordatorios-{pendientes_envio_count}",
            "titulo": "Recordatorios pendientes",
            "detalle": f"{pendientes_envio_count} reserva(s) sin pago por avisar",
            "fecha": timezone.localtime(timezone.now()),
            "url": f"{reverse('admin_agencias_sin_pago')}",
        })

    agencias_panel_qs = (
        Reserva.objects.filter(
            Q(tipo_reserva="agencia")
            | Q(estado__in=ESTADOS_AGENCIA_VISIBLES)
            | ~Q(agencia_nombre="")
        )
        .exclude(estado="cancelada")
        .select_related("salida__tour", "gestionada_por")
        .prefetch_related("pagos")
        .order_by("-fecha_reserva")[:20]
    )
    agencias_panel_rows = []
    for r in agencias_panel_qs:
        tiene_pago = any(p.estado == "paid" for p in r.pagos.all())
        agencias_panel_rows.append({
            "id": r.id,
            "agencia_nombre": r.agencia_nombre or f"{r.nombre} {r.apellidos}".strip(),
            "tour_nombre": (r.salida.tour.nombre if r.salida and r.salida.tour else "-"),
            "monto": r.total_pagar,
            "monto_pagado_agencia": r.monto_pagado_agencia,
            "estado": r.estado,
            "tiene_pago": tiene_pago,
            "gestiona": (r.gestionada_por.username if r.gestionada_por else ""),
            "limite_pago_agencia": r.limite_pago_agencia,
        })

    context = {
        "secretaria_nombre": request.user.get_full_name() or request.user.username,
        "hoy": hoy,
        "show_welcome": bool(request.session.pop("welcome_secretaria", False)),
        "kpis": {
            "reservas_hoy": reservas_hoy_qs.count(),
            "reservas_mes": reservas_mes_qs.count(),
            "bloqueos_agencia": Reserva.objects.filter(estado="bloqueada_por_agencia").count(),
            "ingresos_hoy": pagos_hoy,
            "salidas_hoy": salidas_hoy,
            "solicitudes_agencia_pendientes": solicitudes_agencia_pendientes,
        },
        "salidas_proximas": salidas_proximas,
        "actividad_hoy": actividad_hoy,
        "notificaciones_bloqueos": notificaciones_bloqueos,
        "agencias_panel_rows": agencias_panel_rows,
    }
    return render(request, "core/panel/dashboard_secretaria.html", context)


@login_required
@user_passes_test(es_staff_o_secretaria)
def panel_notificaciones_secretaria_json(request):
    if es_admin(request.user):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    bloqueos_qs = (
        Reserva.objects.filter(estado="solicitud_agencia")
        .select_related("salida__tour")
        .order_by("-fecha_reserva")[:12]
    )
    notificaciones = []
    for r in bloqueos_qs:
        notificaciones.append({
            "notif_id": f"agencia-bloqueo-{r.id}",
            "titulo": f"Bloqueo agencia #{r.id:05d}",
            "detalle": (r.salida.tour.nombre if r.salida and r.salida.tour else "Solicitud de bloqueo"),
            "fecha": timezone.localtime(r.fecha_reserva).strftime("%d/%m/%Y %I:%M %p"),
            "url": f"{reverse('admin_reservas')}?tipo=agencia&estado_agencia=solicitud_agencia",
        })

    hoy = timezone.localdate()
    limite_7d = hoy - timedelta(days=7)
    pendientes_envio_count = (
        Reserva.objects.filter(fecha_reserva__date__gt=limite_7d)
        .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
        .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
        .distinct()
        .count()
    )
    if pendientes_envio_count:
        notificaciones.insert(0, {
            "notif_id": f"agencia-recordatorios-{pendientes_envio_count}",
            "titulo": "Recordatorios pendientes",
            "detalle": f"{pendientes_envio_count} reserva(s) sin pago por avisar",
            "fecha": timezone.localtime(timezone.now()).strftime("%d/%m/%Y %I:%M %p"),
            "url": f"{reverse('admin_agencias_sin_pago')}",
        })

    return JsonResponse({
        "ok": True,
        "notificaciones": notificaciones,
        "generated_at": timezone.localtime(timezone.now()).strftime("%d/%m/%Y %I:%M %p"),
    })


@login_required
def panel_notificaciones_json(request):
    if not (request.user.is_staff or request.user.is_superuser):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    pendientes_agencia = Reserva.objects.filter(estado="solicitud_agencia").count()
    nuevas_reservas = (
        Reserva.objects.select_related("salida__tour")
        .exclude(estado="cancelada")
        .order_by("-fecha_reserva")[:6]
    )
    notificaciones = []
    for r in nuevas_reservas[:5]:
        notificaciones.append({
            "notif_id": f"reserva-{r.id}-{r.estado}",
            "titulo": f"Reserva #{r.id:05d}",
            "detalle": (r.salida.tour.nombre if r.salida and r.salida.tour else "Reserva nueva"),
            "fecha": timezone.localtime(r.fecha_reserva).strftime("%d/%m/%Y %I:%M %p"),
            "url": f"{reverse('admin_reservas')}?tipo=general",
        })
    if pendientes_agencia:
        notificaciones.insert(0, {
            "notif_id": f"agencia-pendiente-{pendientes_agencia}",
            "titulo": "Solicitudes de agencia",
            "detalle": f"{pendientes_agencia} pendientes por revisar",
            "fecha": timezone.localtime(timezone.now()).strftime("%d/%m/%Y %I:%M %p"),
            "url": f"{reverse('admin_reservas')}?tipo=agencia",
        })

    return JsonResponse({
        "ok": True,
        "notificaciones": notificaciones[:10],
        "generated_at": timezone.localtime(timezone.now()).strftime("%d/%m/%Y %I:%M %p"),
    })


@login_required
def factura_agencia_mensual_pdf(request):
    if not es_agencia(request.user):
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    hoy = timezone.localdate()
    inicio_mes = hoy.replace(day=1)
    if hoy.month == 12:
        fin_mes = date(hoy.year + 1, 1, 1) - timedelta(days=1)
    else:
        fin_mes = date(hoy.year, hoy.month + 1, 1) - timedelta(days=1)

    reservas = (
        Reserva.objects.filter(
            fecha_reserva__date__range=[inicio_mes, fin_mes],
        )
        .filter(Q(tipo_reserva="agencia") | Q(estado__in=ESTADOS_AGENCIA_VISIBLES))
        .filter(Q(usuario=request.user) | Q(agencia_correo=request.user.email))
        .exclude(estado__in=["pagada_total_agencia", "pagada", "cancelada", "rechazada_agencia"])
        .select_related("salida__tour")
        .order_by("fecha_reserva")
    )

    empresa = _empresa_config()
    agencia_nombre = request.user.first_name or request.user.username
    periodo_label = hoy.strftime("%B %Y")
    buffer = generar_factura_agencia_mensual_pdf(agencia_nombre, list(reservas), periodo_label, empresa=empresa)

    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="factura_agencia_{hoy.strftime("%Y%m")}.pdf"'
    return response


@login_required
@user_passes_test(es_admin)
def descargar_reporte_rango_pdf(request):
    fecha_desde_raw = (request.GET.get("fecha_desde") or "").strip()
    fecha_hasta_raw = (request.GET.get("fecha_hasta") or "").strip()
    estado_filtro = (request.GET.get("estado") or "todos").strip().lower()
    segmento = (request.GET.get("segmento") or "todos").strip().lower()
    estados_validos = {"todos", "pendiente", "cotizacion_pendiente", "pagada", "cancelada", "solicitud_agencia", "bloqueada_por_agencia"}
    segmentos_validos = {"todos", "usuarios", "secretarias", "agencias"}
    if estado_filtro not in estados_validos:
        estado_filtro = "todos"
    if segmento not in segmentos_validos:
        segmento = "todos"

    hoy = timezone.localdate()
    try:
        fecha_desde = datetime.strptime(fecha_desde_raw, "%Y-%m-%d").date() if fecha_desde_raw else (hoy - timedelta(days=29))
    except ValueError:
        fecha_desde = hoy - timedelta(days=29)
    try:
        fecha_hasta = datetime.strptime(fecha_hasta_raw, "%Y-%m-%d").date() if fecha_hasta_raw else hoy
    except ValueError:
        fecha_hasta = hoy
    if fecha_desde > fecha_hasta:
        fecha_desde, fecha_hasta = fecha_hasta, fecha_desde
    if (fecha_hasta - fecha_desde).days > 62:
        fecha_desde = fecha_hasta - timedelta(days=62)

    pagos = (
        Pago.objects.filter(
            estado="paid",
            creado_en__date__range=[fecha_desde, fecha_hasta],
        )
        .select_related("reserva__salida__tour", "reserva__creado_por", "reserva__usuario")
        .order_by("-creado_en")
    )
    if estado_filtro != "todos":
        pagos = pagos.filter(reserva__estado=estado_filtro)

    if segmento == "usuarios":
        pagos = pagos.filter(reserva__tipo_reserva="general", reserva__usuario__isnull=False)
    elif segmento == "secretarias":
        secretaria_group = Group.objects.filter(name__iexact=GROUP_SECRETARIA).first()
        if secretaria_group:
            secretarias_ids = secretaria_group.user_set.values_list("id", flat=True)
            pagos = pagos.filter(reserva__creado_por_id__in=secretarias_ids)
        else:
            pagos = pagos.none()
    elif segmento == "agencias":
        pagos = pagos.filter(
            Q(reserva__tipo_reserva="agencia")
            | ~Q(reserva__agencia_nombre="")
            | Q(reserva__estado__in=ESTADOS_AGENCIA_VISIBLES)
        )

    items = []
    for pago in pagos:
        res = pago.reserva
        if res and res.creado_por:
            autor = res.creado_por.username
        elif res and res.usuario:
            autor = res.usuario.username
        else:
            autor = "web"
        items.append({
            "tipo": "pago",
            "dt": pago.creado_en,
            "id": pago.id,
            "titulo": f"{res.nombre} {res.apellidos}".strip() if res else "-",
            "tour": (res.salida.tour.nombre if res and res.salida and res.salida.tour else "-"),
            "estado": "paid",
            "monto": pago.monto,
            "usuario": autor,
        })

    resumen = {
        "total_registros": len(items),
        "total_ventas": sum(((item["monto"] or Decimal("0.00")) for item in items), Decimal("0.00")),
    }
    segmento_label = {
        "todos": "todos",
        "usuarios": "usuarios",
        "secretarias": "secretarias",
        "agencias": "agencias",
    }.get(segmento, "todos")
    titulo = (
        f"Reporte operativo del {fecha_desde.strftime('%d/%m/%Y')} al {fecha_hasta.strftime('%d/%m/%Y')} "
        f"(estado: {estado_filtro}, segmento: {segmento_label})"
    )
    buffer = generar_actividad_dia_pdf(titulo, fecha_hasta, items, resumen, _empresa_config())
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="reporte_{segmento_label}_{fecha_desde.strftime("%Y%m%d")}_{fecha_hasta.strftime("%Y%m%d")}.pdf"'
    )
    return response

@login_required
def mis_reservas(request):
    """Vista para que el turista vea su historial de compras/reservas."""
    _cancelar_reservas_agencia_vencidas()
    reservas = list(
        Reserva.objects.filter(usuario=request.user)
        .exclude(estado="cancelada")
        .order_by('-fecha_reserva')
    )
    for reserva in reservas:
        reserva.puede_cancelar_agencia = False
        if (
            _es_reserva_agencia(reserva)
            and reserva.estado == "solicitud_agencia"
            and not reserva.gestionada_por_id
        ):
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
    if reserva.gestionada_por_id:
        messages.error(request, "La solicitud ya fue gestionada por secretaria y no puede cancelarse.")
        return redirect("mis_reservas")
    if reserva.estado != "solicitud_agencia":
        messages.error(request, "Solo puedes cancelar solicitudes de agencia antes de ser aceptadas.")
        return redirect("mis_reservas")

    salida_ref = reserva.salida
    reserva_ref = reserva.id
    reserva.delete()
    _recalcular_disponibilidad_salida(salida_ref)
    messages.success(request, f"Reserva #{reserva_ref:06d} cancelada y removida del historial.")
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

def _tour_price_display(tour, currency_rate, user=None):
    if getattr(tour, "ocultar_precio", False):
        return {
            "adulto": Decimal("0.00"),
            "nino": Decimal("0.00"),
        }
    precio_adulto = tour.precio_adulto_final()
    precio_nino = tour.precio_nino_final() if _aplica_descuento_ninos(tour, user) else precio_adulto
    return {
        "adulto": precio_adulto * currency_rate,
        "nino": precio_nino * currency_rate,
    }


def _get_client_ip(request):
    forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip()


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
        Reserva.objects.filter(
            salida=salida,
            hora_turno_agencia__isnull=False,
            estado__in=ESTADOS_AGENCIA_ACTIVOS,
        )
        .exists()
    )
    if hay_bloqueo_agencia:
        salida.cupos_disponibles = 0
        salida.save(update_fields=["cupos_disponibles"])
        return

    ocupados = (
        Reserva.objects.filter(
            salida=salida,
            estado__in=["pagada", "confirmada", "pagada_total_agencia"] + ESTADOS_AGENCIA_ACTIVOS,
        )
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
    if getattr(reserva, "tipo_reserva", "") == "agencia":
        return True
    if reserva.codigo_agencia or reserva.hora_turno_agencia:
        return True
    if reserva.estado in ESTADOS_AGENCIA_VISIBLES:
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


def _enviar_recordatorio_mensual_agencia(agencia_email, reservas, recordatorio_dt):
    if not agencia_email or not reservas:
        return False

    total_pendiente = sum([r.total_pagar for r in reservas]) if reservas else Decimal("0.00")
    recordatorio_label = timezone.localtime(recordatorio_dt).strftime("%d/%m/%Y")
    periodo_label = timezone.localtime(recordatorio_dt).strftime("%B %Y")
    agencia_nombre = reservas[0].agencia_nombre if reservas else ""

    subject = f"Recordatorio mensual: pagos pendientes de agencia ({recordatorio_label})"
    body = (
        "Hola,\n\n"
        f"Este es un recordatorio mensual. Al {recordatorio_label} tienes pagos pendientes por reservas realizadas con TortugaTur.\n\n"
        f"Total pendiente: ${total_pendiente}\n\n"
        "Adjuntamos la factura mensual con el detalle completo de las reservas del mes.\n"
        "Por favor coordina el pago en efectivo con la secretaria o contáctanos si necesitas ayuda.\n\n"
        "TortugaTur"
    )

    try:
        empresa = _empresa_config()
        pdf_buffer = generar_factura_agencia_mensual_pdf(
            agencia_nombre or "Agencia",
            reservas,
            periodo_label,
            empresa=empresa,
        )
        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[agencia_email],
        )
        email.attach(
            f"factura_agencia_{timezone.localtime(recordatorio_dt).strftime('%Y%m')}.pdf",
            pdf_buffer.getvalue(),
            "application/pdf",
        )
        email.send(fail_silently=False)
        return True
    except Exception:
        logger.exception("No se pudo enviar recordatorio mensual a %s", agencia_email)
        return False


def _notificar_secretarias_solicitud_agencia(reserva):
    group_secretaria = Group.objects.filter(name__iexact=GROUP_SECRETARIA).first()
    correos_secretarias = []
    if group_secretaria:
        correos_secretarias = [
            (u.email or "").strip().lower()
            for u in group_secretaria.user_set.filter(is_active=True)
            if (u.email or "").strip()
        ]

    correo_admin = (getattr(settings, "AGENCIA_EMAIL", "") or "").strip().lower()
    destinatarios = sorted(set(correos_secretarias + ([correo_admin] if correo_admin else [])))
    if not destinatarios:
        return False

    turno = reserva.hora_turno_agencia.strftime("%I:%M %p") if reserva.hora_turno_agencia else "Sin turno"
    subject = f"Nueva solicitud de agencia #{reserva.id:06d}"
    body = (
        "Se registro una nueva solicitud de bloqueo por agencia.\n\n"
        f"Reserva: #{reserva.id:06d}\n"
        f"Agencia: {reserva.usuario.username if reserva.usuario else 'N/A'}\n"
        f"Tour: {reserva.salida.tour.nombre}\n"
        f"Fecha salida: {reserva.salida.fecha.strftime('%Y-%m-%d')}\n"
        f"Turno: {turno}\n"
        f"Pasajeros: {reserva.total_personas()}\n"
        f"Codigo agencia: {reserva.codigo_agencia or 'No registrado'}\n\n"
        "Ingresa al panel de reservas para aceptar o rechazar la solicitud."
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=destinatarios,
            fail_silently=False,
        )
        return True
    except Exception:
        logger.exception("No se pudo enviar notificacion a secretaria para reserva %s", reserva.id)
        return False


def _cancelar_reservas_agencia_vencidas():
    """
    Nuevo flujo agencias:
    - No se cancelan reservas por fecha/hora.
    - Se envia recordatorio mensual 7 dias antes de terminar el mes.
    """
    hoy = timezone.localdate()
    recordatorio_dt = _calcular_limite_pago_agencia(hoy)
    recordatorio_date = timezone.localtime(recordatorio_dt).date()
    if hoy != recordatorio_date:
        return 0

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

    enviados = 0
    agrupadas = defaultdict(list)
    for reserva in reservas_pendientes:
        email = (reserva.agencia_correo or "").strip().lower()
        if not email and reserva.usuario and reserva.usuario.email:
            email = (reserva.usuario.email or "").strip().lower()
        if email:
            agrupadas[email].append(reserva)

    for email, reservas in agrupadas.items():
        reservas_sin_recordatorio = [
            r for r in reservas
            if not r.alerta_24h_agencia_enviada_en
            or r.alerta_24h_agencia_enviada_en.date() != hoy
        ]
        if not reservas_sin_recordatorio:
            continue
        if _enviar_recordatorio_mensual_agencia(email, reservas, recordatorio_dt):
            enviados += 1
            for r in reservas_sin_recordatorio:
                r.alerta_24h_agencia_enviada_en = timezone.now()
                r.save(update_fields=["alerta_24h_agencia_enviada_en"])

    return enviados


def _limpiar_historial_canceladas_agencia_diario():
    """
    Limpieza diaria: elimina del historial las reservas de agencia que
    estén canceladas y pertenezcan a días anteriores.
    """
    hoy = timezone.localdate()
    filtros_agencia = (
        (Q(codigo_agencia__isnull=False) & ~Q(codigo_agencia=""))
        | Q(hora_turno_agencia__isnull=False)
        | Q(tipo_reserva="agencia")
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
        def _clean_email(value):
            email = (value or "").strip().lower()
            return email if "@" in email else ""

        pdf_buffer = generar_ticket_pdf(reserva, _empresa_config())
        pdf_content = pdf_buffer.getvalue()
        pdf_buffer.close()
        subject = f"Confirmacion de Reserva #{reserva.id:06d} - TortugaTur"
        es_agencia_ticket = _es_reserva_agencia(reserva)
        monto_ticket = reserva.total_pagar
        if es_agencia_ticket and (reserva.monto_pagado_agencia or Decimal("0.00")) > 0:
            monto_ticket = reserva.monto_pagado_agencia
        html_body = render_to_string(
            "core/email_ticket.html",
            {
                "reserva": reserva,
                "monto_ticket": monto_ticket,
                "es_agencia_ticket": es_agencia_ticket,
                "empresa": _empresa_config(),
                "site_url": _site_url(request=None),
                "whatsapp_number": getattr(settings, "WHATSAPP_NUMBER", ""),
                "agencia_email": getattr(settings, "AGENCIA_EMAIL", ""),
            },
        )
        recipients = []
        for candidate in [
            _clean_email(getattr(reserva, "correo", "")),
            _clean_email(getattr(getattr(reserva, "usuario", None), "email", "")),
            _clean_email(getattr(reserva, "agencia_correo", "")),
        ]:
            if candidate and candidate not in recipients:
                recipients.append(candidate)

        agencia_email = getattr(settings, "AGENCIA_EMAIL", "")
        if not recipients and not _clean_email(agencia_email):
            return

        to_list = recipients[:] if recipients else []
        agencia_email_clean = _clean_email(agencia_email)
        bcc_list = []
        if agencia_email_clean and agencia_email_clean not in to_list:
            bcc_list.append(agencia_email_clean)

        email_cliente = EmailMessage(
            subject=subject,
            body=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=to_list or [agencia_email_clean],
            bcc=bcc_list,
        )
        email_cliente.content_subtype = "html"
        email_cliente.attach(f"Ticket_TortugaTur_{reserva.id}.pdf", pdf_content, "application/pdf")
        email_cliente.send(fail_silently=True)
    except Exception:
        logger.exception("No se pudo enviar ticket para la reserva %s", reserva.id)


def _send_internal_quote_ready_email(reserva):
    try:
        def _clean_email(value):
            email = (value or "").strip().lower()
            return email if "@" in email else ""

        recipients = []
        for candidate in [
            _clean_email(getattr(reserva, "correo", "")),
            _clean_email(getattr(getattr(reserva, "usuario", None), "email", "")),
        ]:
            if candidate and candidate not in recipients:
                recipients.append(candidate)

        if not recipients:
            return False

        subject = f"Tu cotizacion ya esta lista - Reserva #{reserva.id:06d}"
        html_body = render_to_string(
            "core/email_cotizacion_lista.html",
            {
                "reserva": reserva,
                "empresa": _empresa_config(),
                "site_url": _site_url(request=None),
                "checkout_url": f"{_site_url(request=None)}{reverse('checkout_reserva', args=[reserva.id])}",
                "mis_reservas_url": f"{_site_url(request=None)}{reverse('mis_reservas')}",
                "whatsapp_number": getattr(settings, "WHATSAPP_NUMBER", ""),
            },
        )

        email_cliente = EmailMessage(
            subject=subject,
            body=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=recipients,
        )
        email_cliente.content_subtype = "html"
        email_cliente.send(fail_silently=True)
        return True
    except Exception:
        logger.exception("No se pudo enviar correo de cotizacion lista para la reserva %s", reserva.id)
        return False


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

        if reserva.estado in ["pagada", "pagada_total_agencia"]:
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
        if estado_anterior not in ["bloqueada_por_agencia", "cotizada_agencia", "confirmada_agencia", "pagada_parcial_agencia"]:
            if salida.cupos_disponibles < personas:
                raise ValueError("No hay cupos suficientes al confirmar el pago.")

        reserva.estado = "pagada_total_agencia" if _es_reserva_agencia(reserva) else "pagada"
        if customer_email:
            reserva.correo = customer_email
            reserva.save(update_fields=["estado", "correo"])
        else:
            reserva.save(update_fields=["estado"])

        if estado_anterior not in ["bloqueada_por_agencia", "cotizada_agencia", "confirmada_agencia", "pagada_parcial_agencia"]:
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
    if _es_reserva_agencia(reserva):
        messages.info(request, "Las reservas de agencia no manejan pago en linea.")
        return redirect("mis_reservas")
    embed_mode = (request.GET.get("embed") == "1")
    if reserva.estado == "pagada":
        messages.success(request, "Pago confirmado. Tu reserva ya esta pagada.")
        return redirect(_post_pago_redirect_for_user(request.user, embed_mode=embed_mode))
    if _es_reserva_interna(reserva) and (not reserva.total_pagar or reserva.total_pagar <= 0):
        messages.info(request, "Aun no se ha asignado un valor a esta reserva. Te avisaremos cuando este lista para pago.")
        return redirect("mis_reservas")

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
    embed_mode = (request.GET.get("embed") == "1") or (request.POST.get("embed") == "1")
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if not _puede_gestionar_checkout(request.user, reserva):
        messages.error(request, "No tienes permiso para iniciar este pago.")
        return redirect("home")
    if _es_reserva_agencia(reserva):
        messages.error(request, "Las reservas de agencia no manejan checkout en linea.")
        return redirect("mis_reservas")
    if reserva.estado not in ["pendiente", ESTADO_COTIZACION_PENDIENTE, "bloqueada_por_agencia"]:
        messages.warning(request, "Esta reserva ya no esta pendiente de pago.")
        return redirect("tours")
    if not reserva.total_pagar or reserva.total_pagar <= 0:
        messages.info(request, "Aun no se ha asignado un valor a esta reserva.")
        return redirect("mis_reservas")

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
                    "redirect_url": f"{site_url}{_post_pago_redirect_for_user(request.user, embed_mode=embed_mode)}",
                    "receipt_button_text": "Volver a TortugaTur",
                    "receipt_link_url": f"{site_url}{_post_pago_redirect_for_user(request.user, embed_mode=embed_mode)}",
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
    if _es_reserva_agencia(reserva):
        return JsonResponse({"error": "Las reservas de agencia no manejan pago en linea."}, status=400)
    if reserva.estado not in ["pendiente", ESTADO_COTIZACION_PENDIENTE, "bloqueada_por_agencia"]:
        return JsonResponse({"error": "La reserva ya no esta pendiente de pago."}, status=400)
    if not reserva.total_pagar or reserva.total_pagar <= 0:
        return JsonResponse({"error": "Aun no se ha asignado un valor a esta reserva."}, status=400)

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

    return JsonResponse({"ok": True, "redirect_url": _post_pago_redirect_for_user(request.user, embed_mode=embed_mode)})


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
    fotos_list = Galeria.objects.select_related("tour").all().order_by("tour__nombre", "-id")
    
    if request.method == 'POST':
        tour_id = (request.POST.get("tour") or "").strip()
        tour = Tour.objects.filter(id=tour_id).first() if tour_id else None
        imagen_url = (request.POST.get("imagen_url") or "").strip()
        imagenes = request.FILES.getlist("imagenes")

        if not tour:
            messages.error(request, "Selecciona un tour válido.")
        else:
            created = 0
            for archivo in imagenes:
                Galeria.objects.create(tour=tour, imagen=archivo)
                created += 1
            if imagen_url:
                Galeria.objects.create(tour=tour, imagen_url=imagen_url)
                created += 1

            if created == 0:
                messages.error(request, "Debes subir al menos una imagen o pegar un link.")
            else:
                messages.success(request, f"¡{created} imagen(es) agregada(s) a la galería con éxito!")
                return redirect('panel_galeria')

        form = GaleriaForm(initial={"tour": tour} if tour else None)
    else:
        form = GaleriaForm()
            
    fotos_por_tour = []
    current_label = None
    current_items = []
    for foto in fotos_list:
        label = foto.tour.nombre if foto.tour else "General"
        if current_label is None:
            current_label = label
        if label != current_label:
            fotos_por_tour.append({"label": current_label, "items": current_items})
            current_label = label
            current_items = []
        current_items.append(foto)
    if current_label is not None:
        fotos_por_tour.append({"label": current_label, "items": current_items})

    return render(request, 'core/panel/galeria.html', {
        'fotos': fotos_list,
        'fotos_por_tour': fotos_por_tour,
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
@user_passes_test(es_admin)
@require_POST
def eliminar_galeria_multiple(request):
    from .models import Galeria
    ids = request.POST.getlist("foto_ids")
    if not ids:
        messages.error(request, "No seleccionaste ninguna imagen.")
        return redirect('panel_galeria')
    qs = Galeria.objects.filter(id__in=ids)
    total = qs.count()
    qs.delete()
    messages.success(request, f"{total} imagen(es) eliminada(s).")
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


def _parse_decimal(value):
    raw = (value or "").strip().replace(",", ".")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
        aplica_descuento_ninos = _aplica_descuento_ninos(salida.tour, request.user)
        if ninos > 0 and aplica_descuento_ninos:
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

        precio_adulto = salida.tour.precio_adulto_final()
        if aplica_descuento_ninos:
            total_ninos = sum(_precio_nino_por_edad(edad, tour=salida.tour, user=request.user) for edad in edades_ninos)
        else:
            total_ninos = ninos * precio_adulto
        total_pagar = (adultos * precio_adulto) + total_ninos
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
    # Prioriza la imagen del destino (configurada al crear el destino).
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
        destino_img = getattr(getattr(tour, "destino", None), "imagen_url", "") or ""
        tour.imagen_portada = destino_img or portada_map.get(tour.id, "")
    def _attach_child_prices(t):
        t.child_price_0_2 = _precio_nino_por_edad(0, tour=t, user=request.user)
        t.child_price_3_5 = _precio_nino_por_edad(4, tour=t, user=request.user)
        t.child_price_normal = _precio_nino_por_edad(8, tour=t, user=request.user)
        t.aplica_descuento_ninos = _aplica_descuento_ninos(t, request.user)

    for tour in todos_los_tours:
        _attach_child_prices(tour)
    for tour in list(tours_con_salidas.keys()):
        destino_img = getattr(getattr(tour, "destino", None), "imagen_url", "") or ""
        tour.imagen_portada = destino_img or portada_map.get(tour.id, "")
        _attach_child_prices(tour)
    for tour in list(tours_reserva_directa.keys()):
        destino_img = getattr(getattr(tour, "destino", None), "imagen_url", "") or ""
        tour.imagen_portada = destino_img or portada_map.get(tour.id, "")
        _attach_child_prices(tour)

    # Calendario de disponibilidad (90 días) con tours visibles por día.
    calendar_hoy = timezone.localdate()
    calendar_hasta = calendar_hoy + timedelta(days=90)
    salidas_calendar = (
        SalidaTour.objects.filter(fecha__range=[calendar_hoy, calendar_hasta])
        .values("fecha", "tour_id", "tour__nombre")
        .annotate(
            total_cupos=Sum("cupos_disponibles"),
            max_cupos=Max("cupos_disponibles"),
            total_salidas=Count("id"),
        )
        .order_by("fecha", "tour__nombre")
    )
    salidas_calendar_days = (
        SalidaTour.objects.filter(fecha__range=[calendar_hoy, calendar_hasta])
        .values("fecha")
        .annotate(
            total_cupos=Sum("cupos_disponibles"),
            max_cupos=Max("cupos_disponibles"),
            total_salidas=Count("id"),
        )
        .order_by("fecha")
    )
    calendar_events = []
    for s in salidas_calendar:
        total_cupos = int(s.get("total_cupos") or 0)
        if total_cupos <= 0:
            continue
        max_cupos = int(s.get("max_cupos") or 0)
        color = "#22c55e" if max_cupos > 3 else "#f59e0b"
        tour_name = s.get("tour__nombre") or "Tour"
        calendar_events.append({
            "title": f"{tour_name} · {total_cupos} cupos",
            "start": s["fecha"].isoformat(),
            "allDay": True,
            "backgroundColor": color,
            "borderColor": color,
            "textColor": "#0f172a",
        })
    # Background availability per day (green/amber/red/gray).
    day_map = {d["fecha"]: d for d in salidas_calendar_days}
    cursor = calendar_hoy
    while cursor <= calendar_hasta:
        info = day_map.get(cursor)
        if not info:
            bg = "#e2e8f0"  # no tours
        else:
            total_cupos = int(info.get("total_cupos") or 0)
            if total_cupos <= 0:
                bg = "#ef4444"  # sin cupos
            else:
                max_cupos = int(info.get("max_cupos") or 0)
                bg = "#22c55e" if max_cupos > 3 else "#f59e0b"
        calendar_events.append({
            "start": cursor.isoformat(),
            "allDay": True,
            "display": "background",
            "backgroundColor": bg,
        })
        cursor += timedelta(days=1)

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
            "fecha_hoy_iso": fecha_hoy.isoformat(),
            "salidas_calendar_json": json.dumps(calendar_events),
        },
    )

@require_POST
@login_required
@user_passes_test(es_admin_o_secretaria)
def procesar_pago_efectivo(request, reserva_id):
    _cancelar_reservas_agencia_vencidas()
    embed_mode = (request.POST.get("embed") == "1") or (request.GET.get("embed") == "1")
    reserva = get_object_or_404(Reserva, id=reserva_id)
    if _es_reserva_agencia(reserva):
        messages.error(request, "Las reservas de agencia no manejan cobro en checkout.")
        return redirect("admin_reservas")
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

    def _redir_cancel():
        if embed_mode:
            if es_secretaria(request.user) and not es_admin(request.user):
                return redirect(f"{reverse('secretaria_reservar')}?embed=1")
            return redirect(f"{reverse('admin_reservas')}?embed=1")
        if es_secretaria(request.user) and not es_admin(request.user):
            return redirect("secretaria_reservar")
        if es_admin(request.user):
            return redirect("admin_reservas")
        if es_dueno_turista:
            return redirect("mis_reservas")
        return redirect("home")

    if not (es_admin(request.user) or es_dueno_turista or es_dueno_secretaria):
        messages.error(request, "No tienes permiso para cancelar esta reserva.")
        return _redir_cancel()

    if reserva.estado == "pagada":
        messages.error(request, "No se puede cancelar una reserva ya pagada.")
        return _redir_cancel()

    if _es_reserva_agencia(reserva) and not es_admin_o_secretaria(request.user):
        messages.error(request, "Esta reserva de agencia no puede cancelarse por este medio.")
        return redirect("mis_reservas")

    if es_dueno_turista and not es_admin_o_secretaria(request.user):
        salida_ref = reserva.salida
        reserva_ref = reserva.id
        reserva.delete()
        _recalcular_disponibilidad_salida(salida_ref)
        messages.success(request, f"Reserva #{reserva_ref:06d} cancelada y removida del historial.")
        return _redir_cancel()

    if reserva.estado != "cancelada":
        reserva.estado = "cancelada"
        reserva.save(update_fields=["estado"])
        _recalcular_disponibilidad_salida(reserva.salida)

    messages.success(request, f"Reserva #{reserva.id:06d} cancelada correctamente.")
    return _redir_cancel()

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
    return _redir_agencias(to_top=True)




