from django import forms
from .models import Destino
from .models import Tour
from .models import Galeria, EmpresaConfig
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.core.exceptions import ValidationError
import re

class DestinoForm(forms.ModelForm):
    class Meta:
        model = Destino
        # Solo estos dos campos existen en tu modelo Destino
        fields = ['nombre', 'imagen_url'] 
        widgets = {
            'nombre': forms.TextInput(attrs={
                'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary',
                'placeholder': 'Ej. Isla Santa Cruz'
            }),
            'imagen_url': forms.URLInput(attrs={
                'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary',
                'placeholder': 'https://link-de-la-imagen.jpg'
            }),
        }


#destinotour

class TourForm(forms.ModelForm):
    class Meta:
        model = Tour
        fields = ['nombre', 'destino', 'descripcion', 'duracion', 'precio', 'precio_adulto', 'precio_nino', 'lemonsqueezy_variant_id', 'cupo_maximo', 'hora_turno_1', 'hora_turno_2']
        widgets = {
            'nombre': forms.TextInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'placeholder': 'Nombre del Tour'}),
            'destino': forms.Select(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary'}),
            'descripcion': forms.Textarea(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'rows': 3}),
            'duracion': forms.TextInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'placeholder': 'Ej. Medio día (4 horas)'}),
            'precio': forms.NumberInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'step': '0.01'}),
            'precio_adulto': forms.NumberInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'step': '0.01'}),
            'precio_nino': forms.NumberInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'step': '0.01'}),
            'lemonsqueezy_variant_id': forms.TextInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'placeholder': 'Ej. 987654'}),
            'cupo_maximo': forms.NumberInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary'}),
            'hora_turno_1': forms.TimeInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'type': 'time'}),
            'hora_turno_2': forms.TimeInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'type': 'time'}),
        }

class TuristaLoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].label = "Nombre de Usuario o Correo"
        self.fields['username'].widget.attrs.update({
            'placeholder': 'Ej. juanperez123'
        })
        self.fields['password'].label = "Tu Contraseña"
        self.fields['password'].widget.attrs.update({
            'placeholder': '••••••••'
        })
        
        self.error_messages.update({
            'invalid_login': "Usuario o contraseña incorrectos. Por favor, intenta de nuevo.",
            'inactive': "Tu cuenta ha sido desactivada. Por favor, contacta al administrador.",
        })

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if username is not None and password:
            from django.contrib.auth import authenticate
            
            # Verificar si se ingresó un correo en lugar de un nombre de usuario
            if '@' in username:
                try:
                    user_obj = User.objects.get(email__iexact=username)
                    username = user_obj.username
                except User.DoesNotExist:
                    pass
            
            self.user_cache = authenticate(self.request, username=username, password=password)
            if self.user_cache is None:
                raise self.get_invalid_login_error()
            else:
                self.confirm_login_allowed(self.user_cache)

        return self.cleaned_data

    def confirm_login_allowed(self, user):
        """Verifica si el usuario está activo."""
        if not user.is_active:
            raise forms.ValidationError(
                self.error_messages['inactive'],
                code='inactive',
            )
        super().confirm_login_allowed(user)

class RegistroTuristaForm(UserCreationForm):
    first_name = forms.CharField(
        label="Nombre y Apellido", 
        required=True,
        widget=forms.TextInput(attrs={'placeholder': 'Ej. Juan Pérez'})
    )
    email = forms.EmailField(
        label="Correo Electrónico", 
        required=True,
        widget=forms.EmailInput(attrs={'placeholder': 'ejemplo@correo.com'})
    )

    class Meta:
        model = User
        fields = ['username', 'first_name', 'email']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Mejorando los textos y nombres de los campos para el turista
        self.fields['username'].label = "Nombre de Usuario (Para iniciar sesión)"
        self.fields['username'].help_text = "Sin espacios, solo letras y/o números (ej: juanperez123)."
        self.fields['username'].widget.attrs.update({'placeholder': 'ej: juanperez123'})
        
        # Las contraseñas vienen de UserCreationForm, así que las modificamos aquí
        if 'password1' in self.fields:
            self.fields['password1'].label = "Crea tu Contraseña"
            self.fields['password1'].help_text = "Mínimo 8 caracteres y 1 signo especial (@, $, etc)."
        if 'password2' in self.fields:
            self.fields['password2'].label = "Confirma tu Contraseña"
            self.fields['password2'].help_text = "Vuelve a escribir la misma contraseña."

    def clean_username(self):
        username = self.cleaned_data.get("username")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("¡Ups! Ese nombre de usuario ya está registrado. Por favor, elige uno diferente.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("¡Atención! Este correo electrónico ya está registrado con otra cuenta.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data.get("first_name", "").strip()
        user.email = self.cleaned_data.get("email", "").strip().lower()
        if commit:
            user.save()
        return user

    def clean_password1(self):
        password = self.cleaned_data.get("password1")
        
        # 1. Validar longitud mínima
        if len(password) < 8:
            raise ValidationError("La contraseña debe tener al menos 8 caracteres.")
        
        # 2. Validar caracteres especiales
        if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
            raise ValidationError("La contraseña debe incluir al menos un carácter especial (ej: @, #, $, !).")
            
        return password
    
#formulario
class ContactoForm(forms.Form):
    # Asegúrate de que las opciones del Select coincidan con lo que quieres
    OPCIONES_ASUNTO = [
        ('Reservas', 'Reservas'),
        ('Pagos', 'Pagos y Facturación'),
        ('Quejas', 'Quejas y Sugerencias'),
        ('Otros', 'Otros'),
    ]
    
    nombre = forms.CharField(max_length=100)
    email = forms.EmailField()
    asunto = forms.ChoiceField(choices=OPCIONES_ASUNTO)
    mensaje = forms.CharField(widget=forms.Textarea)

class GaleriaForm(forms.ModelForm):
    class Meta:
        model = Galeria
        fields = ['tour', 'imagen', 'imagen_url']
        widgets = {
            'tour': forms.Select(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary'}),
            'imagen': forms.ClearableFileInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary'}),
            'imagen_url': forms.URLInput(attrs={'class': 'w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary', 'placeholder': 'https://link...'}),
        }


class EmpresaConfigForm(forms.ModelForm):
    class Meta:
        model = EmpresaConfig
        fields = ["nombre_empresa", "ruc", "direccion", "telefono", "correo"]
        widgets = {
            "nombre_empresa": forms.TextInput(attrs={"class": "w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary"}),
            "ruc": forms.TextInput(attrs={"class": "w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary"}),
            "direccion": forms.TextInput(attrs={"class": "w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary"}),
            "telefono": forms.TextInput(attrs={"class": "w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary"}),
            "correo": forms.EmailInput(attrs={"class": "w-full p-3 border border-slate-200 rounded-xl outline-none focus:border-primary"}),
        }
