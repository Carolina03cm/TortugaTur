from django.contrib import admin
from .models import Destino, Tour, SalidaTour, Reserva, Ticket, Resena, Pago, Galeria, UserProfile


@admin.register(Destino)
class DestinoAdmin(admin.ModelAdmin):
    list_display = ("nombre",)


@admin.register(Tour)
class TourAdmin(admin.ModelAdmin):
    list_display = ("nombre", "destino", "precio", "ocultar_precio", "lemonsqueezy_variant_id")


@admin.register(SalidaTour)
class SalidaTourAdmin(admin.ModelAdmin):
    list_display = ("tour", "fecha", "cupo_maximo")


@admin.register(Reserva)
class ReservaAdmin(admin.ModelAdmin):
    list_display = ("id",)  # temporal


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ("id",)  # temporal


@admin.register(Resena)
class ResenaAdmin(admin.ModelAdmin):
    list_display = ("id",)


@admin.register(Pago)
class PagoAdmin(admin.ModelAdmin):
    list_display = ("id", "reserva", "proveedor", "estado", "monto", "moneda", "external_id", "creado_en")

@admin.register(Galeria)
class GaleriaAdmin(admin.ModelAdmin):
    list_display = ('id', 'tour', 'fecha_agregada')
    list_filter = ('tour', 'fecha_agregada')

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "telefono", "is_agencia")
    list_filter = ("is_agencia",)
    search_fields = ("user__username", "user__email")
