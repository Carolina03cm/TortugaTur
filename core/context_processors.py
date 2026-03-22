from django.conf import settings

from .models import SiteVisit


def whatsapp_number(request):
    return {"WHATSAPP_NUMBER": getattr(settings, "WHATSAPP_NUMBER", "")}


def site_visit_count(request):
    return {"SITE_VISIT_COUNT": SiteVisit.objects.count()}
