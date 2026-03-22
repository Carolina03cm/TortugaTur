from django.shortcuts import redirect
from django.urls import reverse
from django.conf import settings
from uuid import uuid4

from .models import SiteVisit, UserProfile
from .ip_utils import normalize_ip

VISITOR_COOKIE_NAME = "tt_visitor_id"


class ForcePasswordChangeMiddleware:
    """
    Si el perfil tiene force_password_change=True, redirige a perfil
    hasta que cambie su contraseña.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        login_url = reverse("login")
        logout_url = reverse("logout")
        home_url = reverse("home")
        panel_url = reverse("panel_admin")
        perfil_url = reverse("perfil_admin")
        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            if request.path == login_url:
                if user.is_staff or user.is_superuser:
                    return redirect(panel_url)
                return redirect(home_url)
            if request.path not in {perfil_url, logout_url}:
                static_url = getattr(settings, "STATIC_URL", "/static/")
                media_url = getattr(settings, "MEDIA_URL", "/media/")
                if not (request.path.startswith(static_url) or request.path.startswith(media_url)):
                    force_change = UserProfile.objects.filter(user=user).values_list("force_password_change", flat=True).first()
                    if force_change:
                        return redirect(perfil_url)

        if request.method == "GET":
            static_url = getattr(settings, "STATIC_URL", "/static/")
            media_url = getattr(settings, "MEDIA_URL", "/media/")
            new_visitor_key = ""
            if not request.path.startswith((static_url, media_url, "/admin")):
                ip_address = self._get_client_ip(request)
                visitor_key = (request.COOKIES.get(VISITOR_COOKIE_NAME) or "").strip()
                if not visitor_key:
                    visitor_key = uuid4().hex
                    new_visitor_key = visitor_key
                visit, created = SiteVisit.objects.get_or_create(
                    visitor_key=visitor_key,
                    defaults={"ip_address": ip_address},
                )
                if not created and ip_address and visit.ip_address != ip_address:
                    visit.ip_address = ip_address
                    visit.save(update_fields=["ip_address", "last_seen"])

        response = self.get_response(request)

        if request.method == "GET" and 'new_visitor_key' in locals() and new_visitor_key:
            response.set_cookie(
                VISITOR_COOKIE_NAME,
                new_visitor_key,
                max_age=60 * 60 * 24 * 365,
                httponly=True,
                samesite="Lax",
            )

        should_disable_cache = (user and user.is_authenticated) or request.path in {login_url, logout_url}
        if should_disable_cache:
            # Evita que el navegador reutilice vistas privadas o de autenticacion con el boton atras.
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"

        return response

    def _get_client_ip(self, request):
        forwarded_for = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
        if forwarded_for:
            return normalize_ip(forwarded_for.split(",")[0].strip())
        real_ip = (request.META.get("HTTP_X_REAL_IP") or "").strip()
        if real_ip:
            return normalize_ip(real_ip)
        return normalize_ip(request.META.get("REMOTE_ADDR"))
