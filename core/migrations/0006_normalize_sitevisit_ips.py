from django.db import migrations
import ipaddress


def normalize_ip(raw_ip):
    raw_ip = (raw_ip or "").strip()
    if not raw_ip:
        return ""

    candidate = raw_ip
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1:candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]

    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]

    try:
        ip_obj = ipaddress.ip_address(candidate)
    except ValueError:
        return ""

    ipv4_mapped = getattr(ip_obj, "ipv4_mapped", None)
    if ipv4_mapped:
        return str(ipv4_mapped)
    return ip_obj.compressed


def normalize_site_visits(apps, schema_editor):
    SiteVisit = apps.get_model("core", "SiteVisit")
    seen = {}

    for visit in SiteVisit.objects.all().order_by("first_seen", "id"):
        normalized = normalize_ip(visit.ip_address)
        if not normalized:
            visit.delete()
            continue

        existing_id = seen.get(normalized)
        if existing_id:
            existing = SiteVisit.objects.get(id=existing_id)
            if visit.first_seen < existing.first_seen:
                existing.first_seen = visit.first_seen
            if visit.last_seen > existing.last_seen:
                existing.last_seen = visit.last_seen
            existing.save(update_fields=["first_seen", "last_seen"])
            visit.delete()
            continue

        duplicate = SiteVisit.objects.filter(ip_address=normalized).exclude(id=visit.id).order_by("first_seen", "id").first()
        if duplicate:
            if visit.first_seen < duplicate.first_seen:
                duplicate.first_seen = visit.first_seen
            if visit.last_seen > duplicate.last_seen:
                duplicate.last_seen = visit.last_seen
            duplicate.save(update_fields=["first_seen", "last_seen"])
            visit.delete()
            seen[normalized] = duplicate.id
            continue

        if visit.ip_address != normalized:
            visit.ip_address = normalized
            visit.save(update_fields=["ip_address"])
        seen[normalized] = visit.id


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_delete_tourvisit"),
    ]

    operations = [
        migrations.RunPython(normalize_site_visits, migrations.RunPython.noop),
    ]
