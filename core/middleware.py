from django.shortcuts import redirect
from django.urls import reverse


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

        user = getattr(request, "user", None)
        if user and user.is_authenticated:
            if request.path == login_url:
                if user.is_superuser or user.is_staff or user.groups.filter(name__iexact="secretaria").exists():
                    return redirect(panel_url)
                return redirect(home_url)

            try:
                perfil = user.perfil
            except Exception:
                perfil = None

            if perfil and getattr(perfil, "force_password_change", False):
                perfil_url = reverse("perfil_admin")
                allowed = {
                    perfil_url,
                    logout_url,
                    login_url,
                }
                if request.path not in allowed:
                    return redirect("perfil_admin")

        response = self.get_response(request)

        should_disable_cache = (user and user.is_authenticated) or request.path in {login_url, logout_url}
        if should_disable_cache:
            # Evita que el navegador reutilice vistas privadas o de autenticacion con el boton atras.
            response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, private"
            response["Pragma"] = "no-cache"
            response["Expires"] = "0"

        return response
