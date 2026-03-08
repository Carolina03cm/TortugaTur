from core.models import Pago


def run():
    pagos = Pago.objects.filter(proveedor="lemonsqueezy").order_by("-id")[:5]
    for p in pagos:
        print(f"Pago ID: {p.id}")
        print(f"Estado Pago: {p.estado}")
        print(f"Reserva ID: {p.reserva.id}")
        print(f"Estado Reserva: {p.reserva.estado}")
        if isinstance(p.payload, dict):
            # Webhook payload has 'meta' and 'data'. API payload has 'data'.
            print(f"Payload meta custom_data: {p.payload.get('meta', {}).get('custom_data', {})}")
            print(f"Payload data attributes checkout_data custom: {p.payload.get('data', {}).get('attributes', {}).get('checkout_data', {}).get('custom', {})}")
        print("-" * 40)


if __name__ == "__main__":
    run()
