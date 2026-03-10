from django.db import models

from django.contrib.auth.models import User
from django.utils import timezone





class Destino(models.Model):
    nombre = models.CharField(max_length=100)
    imagen_url = models.URLField("Imagen (URL)", max_length=500)

    def __str__(self):
        return self.nombre

class Tour(models.Model):
    nombre = models.CharField(max_length=150)
    destino = models.ForeignKey(Destino, on_delete=models.CASCADE, related_name="tours")
    descripcion = models.TextField()
    precio = models.DecimalField(max_digits=8, decimal_places=2)
    precio_adulto = models.DecimalField(max_digits=8, decimal_places=2, default=0, blank=True)
    precio_nino = models.DecimalField(max_digits=8, decimal_places=2, default=0, blank=True)
    lemonsqueezy_variant_id = models.CharField(max_length=50, blank=True, default="")
    # Nota: Los campos cupo_maximo y disponibles aquí suelen ser una referencia general
    cupo_maximo = models.PositiveIntegerField(default=16)
    cupos_disponibles = models.PositiveIntegerField(default=16)
    duracion = models.CharField(max_length=100, blank=True, null=True, verbose_name="Duración del tour", help_text="Ej: 4 horas, Medio día, etc.")
    
    # Horarios automáticos cada día
    hora_turno_1 = models.TimeField(null=True, blank=True, verbose_name="Hora Turno 1")
    hora_turno_2 = models.TimeField(null=True, blank=True, verbose_name="Hora Turno 2")
    descuento_ninos_activo = models.BooleanField(
        default=True,
        verbose_name="Aplicar descuento a ninos",
        help_text="Si se desactiva, los ninos pagan tarifa de adulto.",
    )
    descuento_ninos_agencia_activo = models.BooleanField(
        default=False,
        verbose_name="Aplicar descuento ninos (agencias)",
        help_text="Control independiente para proceso de agencias.",
    )
    visible_para_agencias = models.BooleanField(
        default=True,
        verbose_name="Visible para agencias",
        help_text="Si se desactiva, este tour no aparecera para cuentas de agencia.",
    )

    def __str__(self):
        return f"{self.nombre} - {self.destino.nombre}"

    def precio_adulto_final(self):
        return self.precio_adulto if self.precio_adulto and self.precio_adulto > 0 else self.precio

    def precio_nino_final(self):
        return self.precio_nino if self.precio_nino and self.precio_nino > 0 else self.precio

class SalidaTour(models.Model):
    tour = models.ForeignKey(Tour, on_delete=models.CASCADE, related_name="salidas")
    fecha = models.DateField()
    # --- CAMBIO: Se agrega el horario ---
    hora = models.TimeField(null=True, blank=True) 
    cupo_maximo = models.PositiveIntegerField(default=16)
    cupos_disponibles = models.PositiveIntegerField(default=16)
    duracion = models.CharField(max_length=100, blank=True, null=True, verbose_name="Duración", help_text="Ej: Medio día (4 horas)")
    creado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="salidas_creadas")

    def __str__(self):
        # Mostramos la hora en el string para identificarla en el admin
        hora_str = self.hora.strftime('%I:%M %p') if self.hora else "Sin hora"
        return f"{self.tour.nombre} - {self.fecha} ({hora_str})"

    def hay_cupo(self, adultos, ninos):
        total = adultos + ninos
        return self.cupos_disponibles >= total

class Reserva(models.Model):
    TIPOS_RESERVA = (
        ("general", "Reserva General"),
        ("agencia", "Reserva de Agencia"),
    )

    ESTADOS = (
        ("pendiente", "Pendiente"),
        ("solicitud_agencia", "Solicitud Agencia"),
        ("cotizada_agencia", "Cotizada Agencia"),
        ("confirmada_agencia", "Confirmada Agencia"),
        ("pagada_parcial_agencia", "Pagada Parcial Agencia"),
        ("pagada_total_agencia", "Pagada Total Agencia"),
        ("rechazada_agencia", "Rechazada Agencia"),
        ("confirmada", "Confirmada"),
        ("pagada", "Pagada"),
        ("cancelada", "Cancelada"),
        ("bloqueada_por_agencia", "Bloqueada por Agencia"),
    )

    usuario = models.ForeignKey(User, on_delete=models.CASCADE, null=True, blank=True)
    salida = models.ForeignKey(SalidaTour, on_delete=models.CASCADE, related_name="reservas")
    adultos = models.PositiveIntegerField()
    ninos = models.PositiveIntegerField()
    total_pagar = models.DecimalField(max_digits=10, decimal_places=2)
    estado = models.CharField(max_length=30, choices=ESTADOS, default="pendiente")
    tipo_reserva = models.CharField(
        max_length=20,
        choices=TIPOS_RESERVA,
        default="general",
    )
    fecha_reserva = models.DateTimeField(default=timezone.now)
    creado_por = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reservas_creadas")
    gestionada_por = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservas_gestionadas",
    )
    
    # Nuevos campos para tracking de agencias
    archivo_agencia = models.FileField(upload_to='agencia_vouchers/', null=True, blank=True)
    codigo_agencia = models.CharField(max_length=50, null=True, blank=True)
    limite_pago_agencia = models.DateTimeField(null=True, blank=True)
    alerta_24h_agencia_enviada_en = models.DateTimeField(null=True, blank=True)
    hora_turno_agencia = models.TimeField(null=True, blank=True)
    hora_turno_libre = models.TimeField(null=True, blank=True)
    agencia_nombre = models.CharField(max_length=150, blank=True, default="")
    agencia_contacto = models.CharField(max_length=120, blank=True, default="")
    agencia_telefono = models.CharField(max_length=30, blank=True, default="")
    agencia_correo = models.EmailField(blank=True, default="")
    monto_pagado_agencia = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    observaciones_agencia = models.TextField(blank=True, default="")

    # Datos del cliente
    nombre = models.CharField(max_length=100)
    apellidos = models.CharField(max_length=150)
    correo = models.EmailField()
    telefono = models.CharField(max_length=30)
    identificacion = models.CharField(max_length=50)

    def total_personas(self):
        return self.adultos + self.ninos


class Pago(models.Model):
    PROVEEDORES = (
        ("lemonsqueezy", "Lemon Squeezy"),
        ("paypal", "PayPal"),
        ("efectivo", "Efectivo"),
    )
    ESTADOS = (
        ("created", "Created"),
        ("approved", "Approved"),
        ("paid", "Paid"),
        ("failed", "Failed"),
        ("canceled", "Canceled"),
    )

    reserva = models.ForeignKey(Reserva, on_delete=models.CASCADE, related_name="pagos")
    proveedor = models.CharField(max_length=20, choices=PROVEEDORES)
    estado = models.CharField(max_length=20, choices=ESTADOS, default="created")
    moneda = models.CharField(max_length=3, default="USD")
    monto = models.DecimalField(max_digits=10, decimal_places=2)
    external_id = models.CharField(max_length=120, blank=True)
    checkout_url = models.URLField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-creado_en",)

    def __str__(self):
        return f"{self.proveedor} #{self.id} - Reserva {self.reserva_id}"

# Los modelos Ticket y Resena se mantienen igual...

class Ticket(models.Model):
    reserva = models.OneToOneField(Reserva, on_delete=models.CASCADE, related_name="ticket")
    codigo = models.CharField(max_length=50, unique=True)
    fecha_emision = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Ticket {self.codigo}"


class Resena(models.Model):
    usuario = models.ForeignKey(User, on_delete=models.CASCADE)
    tour = models.ForeignKey(Tour, on_delete=models.CASCADE, related_name="resenas")
    puntuacion = models.PositiveIntegerField()  # 1 a 5
    comentario = models.TextField()
    fecha = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.tour.nombre} - {self.puntuacion}⭐"

#imagenes
class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="perfil")
    foto = models.ImageField(upload_to="perfiles/", blank=True, null=True, help_text="Foto de perfil")
    telefono = models.CharField(max_length=20, blank=True, null=True)
    biografia = models.TextField(blank=True, null=True)
    cedula = models.CharField(max_length=20, blank=True, default="")
    force_password_change = models.BooleanField(default=False)
    is_agencia = models.BooleanField(default=False, verbose_name="Es agencia de tours", help_text="Si se activa, el usuario podrá bloquear reservas por 15 días")

    def __str__(self):
        return f"Perfil de {self.user.username}"

class Galeria(models.Model):
    tour = models.ForeignKey(Tour, on_delete=models.CASCADE, related_name='fotos', null=True, blank=True)
    imagen = models.ImageField(upload_to='galeria_tours/', blank=True, null=True, help_text="Sube una foto local (desde tu PC)")
    imagen_url = models.URLField(max_length=500, blank=True, null=True, help_text="O pega el enlace de Drive/Photos/Internet")
    fecha_agregada = models.DateTimeField(auto_now_add=True)

    def obtener_imagen_url(self):
        if self.imagen:
            return self.imagen.url
        
        if self.imagen_url:
            import re
            # Si es un link de Google Drive (tipo /file/d/ID/view)
            m = re.search(r'/file/d/([a-zA-Z0-9_-]+)', self.imagen_url)
            if m:
                return f"https://drive.google.com/uc?export=view&id={m.group(1)}"
            
            # Si es un link de Google Drive (tipo /open?id=ID)
            m2 = re.search(r'id=([a-zA-Z0-9_-]+)', self.imagen_url)
            if m2 and 'drive.google.com' in self.imagen_url:
                return f"https://drive.google.com/uc?export=view&id={m2.group(1)}"
            
            return self.imagen_url
        return ""

    def __str__(self):
        return f"Foto de {self.tour.nombre if self.tour else 'Galería'} - {self.fecha_agregada.strftime('%Y-%m-%d')}"

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        try:
            old_inst = Galeria.objects.get(pk=self.pk) if not is_new else None
        except Galeria.DoesNotExist:
            old_inst = None

        super().save(*args, **kwargs)

        if self.imagen:
            # Solo si se acaba de crear el registro, o si cambió la imagen frente al anterior
            if is_new or (old_inst and old_inst.imagen != self.imagen):
                self._aplicar_marca_agua()

    def _aplicar_marca_agua(self):
        try:
            from PIL import Image, ImageDraw, ImageFont
            from django.conf import settings
            import os

            if not self.imagen or not getattr(self.imagen, 'path', None):
                return
            
            filepath = self.imagen.path
            if not os.path.exists(filepath):
                return
                
            img = Image.open(filepath)
            original_mode = img.mode
            img = img.convert('RGBA')

            txt = Image.new('RGBA', img.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(txt)
            
            text = "TortugaTur"
            width, height = img.size
            # Compact watermark text.
            fontsize = max(int(width / 62), 8)

            try:
                import platform
                if platform.system() == "Windows":
                    font = ImageFont.truetype("arial.ttf", fontsize)
                else:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", fontsize)
            except Exception:
                font = ImageFont.load_default()

            try:
                if hasattr(draw, "textbbox"):
                    text_bbox = draw.textbbox((0, 0), text, font=font)
                    text_width = text_bbox[2] - text_bbox[0]
                    text_height = text_bbox[3] - text_bbox[1]
                else:
                    text_width, text_height = draw.textsize(text, font=font)
            except Exception:
                text_width, text_height = (fontsize * len(text) // 1.5, fontsize)

            margin_right = int(width * 0.018)
            margin_bottom = int(height * 0.018)

            # Optional static logo watermark.
            logo = None
            logo_width = 0
            logo_height = 0
            logo_gap = max(int(width * 0.004), 4)
            possible_logo_paths = [
                os.path.join(settings.BASE_DIR, "core", "static", "icon", "logo.png"),
                os.path.join(settings.BASE_DIR, "static", "icon", "logo.png"),
            ]
            for logo_path in possible_logo_paths:
                if os.path.exists(logo_path):
                    try:
                        logo = Image.open(logo_path).convert("RGBA")
                        logo_width = max(min(int(width * 0.035), 42), 20)
                        ratio = logo_width / float(max(logo.size[0], 1))
                        logo_height = max(int(logo.size[1] * ratio), 10)
                        logo = logo.resize((logo_width, logo_height), Image.LANCZOS)

                        # Subtle but readable opacity.
                        alpha = logo.split()[3].point(lambda p: int(p * 0.36))
                        logo.putalpha(alpha)
                    except Exception:
                        logo = None
                    break

            content_w = text_width + (logo_gap + logo_width if logo else 0)
            content_h = max(text_height, logo_height)
            text_x = width - content_w - margin_right
            text_y = height - content_h - margin_bottom + max((content_h - text_height) // 2, 0)
            logo_x = text_x + text_width + logo_gap
            logo_y = height - content_h - margin_bottom + max((content_h - logo_height) // 2, 0)

            # Minimal contrast bump for readability on bright sand/sky.
            draw.text((text_x + 1, text_y + 1), text, font=font, fill=(0, 0, 0, 55))
            color_texto = (245, 248, 250, 145)
            draw.text((text_x, text_y), text, font=font, fill=color_texto)

            if logo:
                txt.alpha_composite(logo, dest=(logo_x, logo_y))

            watermarked = Image.alpha_composite(img, txt)
            
            if filepath.lower().endswith(('.jpg', '.jpeg')):
                watermarked = watermarked.convert('RGB')
                watermarked.save(filepath, quality=90)
            else:
                watermarked = watermarked.convert(original_mode)
                watermarked.save(filepath)

        except Exception as e:
            print(f"Error al aplicar marca de agua: {e}")


class EmpresaConfig(models.Model):
    nombre_empresa = models.CharField(max_length=150, default="TortugaTur")
    ruc = models.CharField(max_length=30, blank=True, default="")
    direccion = models.CharField(max_length=255, blank=True, default="")
    telefono = models.CharField(max_length=50, blank=True, default="")
    correo = models.EmailField(blank=True, default="")

    class Meta:
        verbose_name = "Configuracion de Empresa"
        verbose_name_plural = "Configuracion de Empresa"

    def __str__(self):
        return f"{self.nombre_empresa} ({self.ruc or 'Sin RUC'})"

