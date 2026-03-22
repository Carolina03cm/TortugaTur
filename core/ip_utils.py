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
