from io import BytesIO
import hashlib
from decimal import Decimal
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.graphics.barcode import code128
from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Paragraph, Spacer


def _safe_text(value, default="-"):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _access_key(reserva, empresa_ruc):
    seed = f"{empresa_ruc}|{reserva.id}|{reserva.fecha_reserva.isoformat()}|{reserva.total_pagar}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest().upper()
    return f"{reserva.fecha_reserva.strftime('%Y%m%d')}{digest[:24]}"


def _es_reserva_agencia(reserva):
    estado = (getattr(reserva, "estado", "") or "").lower()
    return bool(
        (getattr(reserva, "tipo_reserva", "") == "agencia")
        or (getattr(reserva, "codigo_agencia", "") or "").strip()
        or getattr(reserva, "hora_turno_agencia", None)
        or (getattr(reserva, "agencia_nombre", "") or "").strip()
        or estado in {
            "solicitud_agencia",
            "cotizada_agencia",
            "confirmada_agencia",
            "pagada_parcial_agencia",
            "pagada_total_agencia",
            "rechazada_agencia",
            "bloqueada_por_agencia",
        }
    )


def generar_ticket_pdf(reserva, empresa=None):
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter

    color_primary = colors.HexColor("#0F172A")
    color_secondary = colors.HexColor("#0EA5A5")
    color_light = colors.HexColor("#F8FAFC")
    color_border = colors.HexColor("#CBD5E1")
    color_text = colors.HexColor("#0F172A")
    color_muted = colors.HexColor("#64748B")

    margin_x = 34

    empresa_nombre = "TortugaTur"
    empresa_ruc = ""
    empresa_direccion = ""
    empresa_telefono = ""
    empresa_correo = ""
    if empresa is not None:
        empresa_nombre = getattr(empresa, "nombre_empresa", "") or empresa_nombre
        empresa_ruc = getattr(empresa, "ruc", "") or ""
        empresa_direccion = getattr(empresa, "direccion", "") or ""
        empresa_telefono = getattr(empresa, "telefono", "") or ""
        empresa_correo = getattr(empresa, "correo", "") or ""

    hora_salida = reserva.salida.hora.strftime("%I:%M %p") if reserva.salida.hora else "Por definir"
    fecha_emision = reserva.fecha_reserva.strftime("%d/%m/%Y %I:%M %p")
    clave_acceso = _access_key(reserva, empresa_ruc)
    estado_text = (reserva.estado or "pendiente").upper()

    # Header
    p.setFillColor(color_primary)
    p.roundRect(20, height - 128, width - 40, 100, 12, fill=1, stroke=0)

    p.setFillColor(colors.white)
    p.setFont("Helvetica-Bold", 22)
    p.drawString(margin_x, height - 66, empresa_nombre.upper())

    p.setFont("Helvetica-Bold", 10)
    if empresa_ruc:
        p.drawString(margin_x, height - 84, f"RUC: {empresa_ruc}")
    else:
        p.drawString(margin_x, height - 84, "RUC: No configurado")

    p.setFont("Helvetica", 9)
    p.drawString(margin_x, height - 98, f"Direccion: {_safe_text(empresa_direccion)}")
    p.drawString(margin_x, height - 110, f"Telefono: {_safe_text(empresa_telefono)}")
    p.drawString(margin_x, height - 122, f"Correo: {_safe_text(empresa_correo)}")

    p.setFont("Helvetica-Bold", 13)
    p.drawRightString(width - margin_x, height - 58, "COMPROBANTE DE RESERVA")
    p.setFont("Helvetica", 11)
    p.drawRightString(width - margin_x, height - 76, f"No: {reserva.id:06d}")
    p.drawRightString(width - margin_x, height - 92, f"Emision: {fecha_emision}")
    p.drawRightString(width - margin_x, height - 108, f"Estado: {estado_text}")

    # Top blocks
    left_w = 312
    right_w = width - (margin_x * 2) - left_w - 14
    block_h = 150
    top_y = height - 298

    p.setFillColor(color_light)
    p.setStrokeColor(color_border)
    p.roundRect(margin_x, top_y, left_w, block_h, 10, fill=1, stroke=1)

    p.setFillColor(color_primary)
    p.setFont("Helvetica-Bold", 10)
    p.drawString(margin_x + 12, top_y + block_h - 20, "DATOS DE CLIENTE")
    p.setStrokeColor(color_secondary)
    p.line(margin_x + 12, top_y + block_h - 24, margin_x + left_w - 12, top_y + block_h - 24)

    p.setFillColor(color_text)
    p.setFont("Helvetica", 9.5)
    nombre_cliente = f"{_safe_text(reserva.nombre)} {_safe_text(reserva.apellidos, '')}".strip()
    p.drawString(margin_x + 12, top_y + block_h - 42, f"Nombre: {nombre_cliente}")
    p.drawString(margin_x + 12, top_y + block_h - 57, f"Identificacion: {_safe_text(reserva.identificacion)}")
    p.drawString(margin_x + 12, top_y + block_h - 72, f"Telefono: {_safe_text(reserva.telefono)}")
    p.drawString(margin_x + 12, top_y + block_h - 87, f"Correo: {_safe_text(reserva.correo)}")
    p.drawString(margin_x + 12, top_y + block_h - 102, f"Fecha de reserva: {reserva.fecha_reserva.strftime('%d/%m/%Y')}")

    x_right = margin_x + left_w + 14
    p.setFillColor(color_light)
    p.setStrokeColor(color_border)
    p.roundRect(x_right, top_y, right_w, block_h, 10, fill=1, stroke=1)

    p.setFillColor(color_primary)
    p.setFont("Helvetica-Bold", 10)
    p.drawString(x_right + 10, top_y + block_h - 20, "CLAVE DE ACCESO")
    p.setStrokeColor(color_secondary)
    p.line(x_right + 10, top_y + block_h - 24, x_right + right_w - 10, top_y + block_h - 24)

    # Fit barcode to the available width so it never overflows the access box.
    barcode = code128.Code128(clave_acceso, barHeight=32, barWidth=0.72)
    barcode_x = x_right + 10
    barcode_y = top_y + 72
    max_barcode_width = right_w - 20
    if barcode.width > max_barcode_width:
        scale_x = max_barcode_width / float(barcode.width)
        p.saveState()
        p.translate(barcode_x, barcode_y)
        p.scale(scale_x, 1)
        barcode.drawOn(p, 0, 0)
        p.restoreState()
    else:
        barcode.drawOn(p, barcode_x, barcode_y)
    p.setFillColor(color_muted)
    p.setFont("Helvetica", 7.5)
    p.drawString(x_right + 10, top_y + 64, clave_acceso)

    p.setFillColor(color_text)
    p.setFont("Helvetica", 9)
    p.drawString(x_right + 10, top_y + 44, f"Tour: {_safe_text(reserva.salida.tour.nombre)}")
    p.drawString(x_right + 10, top_y + 30, f"Destino: {_safe_text(reserva.salida.tour.destino.nombre)}")
    p.drawString(
        x_right + 10,
        top_y + 16,
        f"Salida: {reserva.salida.fecha.strftime('%d/%m/%Y')} {hora_salida}",
    )

    es_agencia = _es_reserva_agencia(reserva)
    monto_pagado_agencia = getattr(reserva, "monto_pagado_agencia", None) or Decimal("0.00")
    total_factura = Decimal(reserva.total_pagar or 0)
    if es_agencia and monto_pagado_agencia > 0:
        total_factura = Decimal(monto_pagado_agencia)

    # Detail table
    if es_agencia:
        data = [["Codigo", "Descripcion", "Cant.", "Monto"]]
        data.append([
            "AG01",
            f"Reserva de agencia - {reserva.salida.tour.nombre}",
            "1",
            f"{float(total_factura):.2f}",
        ])
        data.append(["", "", "TOTAL A COBRAR USD", f"{float(total_factura):.2f}"])
        row_heights = [24, 22, 26]
        table = Table(data, colWidths=[64, 306, 84, 86], rowHeights=row_heights)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), color_primary),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 0), (3, -1), "RIGHT"),
            ("ALIGN", (1, 0), (1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -2), 0.5, color_border),
            ("LINEABOVE", (0, -1), (-1, -1), 1, color_secondary),
            ("FONTNAME", (2, -1), (3, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (2, -1), (3, -1), color_primary),
            ("BACKGROUND", (0, -1), (-1, -1), color_light),
        ]
        table.setStyle(TableStyle(style))
    else:
        precio_adulto = reserva.salida.tour.precio_adulto_final()
        precio_nino = reserva.salida.tour.precio_nino_final()
        subtotal_adultos = reserva.adultos * precio_adulto
        subtotal_ninos = reserva.ninos * precio_nino

        data = [["Codigo", "Descripcion", "Cant.", "P. Unitario", "Subtotal"]]
        if reserva.adultos > 0:
            data.append([
                "A001",
                f"Adulto - {reserva.salida.tour.nombre}",
                str(reserva.adultos),
                f"{float(precio_adulto):.2f}",
                f"{float(subtotal_adultos):.2f}",
            ])
        if reserva.ninos > 0:
            data.append([
                "N001",
                "Nino (tarifa segun edad)",
                str(reserva.ninos),
                f"{float(precio_nino):.2f}",
                f"{float(subtotal_ninos):.2f}",
            ])
        data.append(["", "", "", "TOTAL A COBRAR USD", f"{float(total_factura):.2f}"])

        row_heights = [24] + [22] * (len(data) - 2) + [26]
        table = Table(data, colWidths=[64, 246, 50, 90, 90], rowHeights=row_heights)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), color_primary),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (2, 0), (2, -1), "CENTER"),
            ("ALIGN", (3, 0), (4, -1), "RIGHT"),
            ("ALIGN", (1, 0), (1, -1), "LEFT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -2), 0.5, color_border),
            ("LINEABOVE", (0, -1), (-1, -1), 1, color_secondary),
            ("FONTNAME", (3, -1), (4, -1), "Helvetica-Bold"),
            ("TEXTCOLOR", (3, -1), (4, -1), color_primary),
            ("BACKGROUND", (0, -1), (-1, -1), color_light),
        ]
        table.setStyle(TableStyle(style))

    table_height = sum(row_heights)
    table_y = top_y - 18 - table_height
    table.wrapOn(p, margin_x, table_y)
    table.drawOn(p, margin_x, table_y)

    # Summary box
    summary_y = table_y - 72
    p.setStrokeColor(color_border)
    p.roundRect(width - margin_x - 210, summary_y, 210, 62, 8, fill=0, stroke=1)
    p.setFont("Helvetica", 9)
    p.setFillColor(color_muted)
    p.drawString(width - margin_x - 198, summary_y + 42, "Subtotal")
    p.drawString(width - margin_x - 198, summary_y + 28, "Descuento")
    p.drawString(width - margin_x - 198, summary_y + 14, "Total")
    p.setFillColor(color_text)
    total_float = float(total_factura)
    p.drawRightString(width - margin_x - 10, summary_y + 42, f"{total_float:.2f} USD")
    p.drawRightString(width - margin_x - 10, summary_y + 28, "0.00 USD")
    p.setFont("Helvetica-Bold", 10)
    p.drawRightString(width - margin_x - 10, summary_y + 14, f"{total_float:.2f} USD")

    # Footer
    p.setStrokeColor(color_border)
    p.line(margin_x, 52, width - margin_x, 52)
    p.setFillColor(color_muted)
    p.setFont("Helvetica-Oblique", 8.3)
    p.drawString(
        margin_x,
        40,
        "Documento de uso interno para reserva de tour. No reemplaza comprobante tributario oficial.",
    )
    p.setFont("Helvetica", 8)
    p.drawRightString(width - margin_x, 40, f"Generado: {fecha_emision}")

    p.showPage()
    p.save()
    buffer.seek(0)
    return buffer


def generar_actividad_dia_pdf(titulo, fecha, items, resumen, empresa=None):
    buffer = BytesIO()

    color_primary = colors.HexColor("#0F172A")
    color_border = colors.HexColor("#CBD5E1")
    color_light = colors.HexColor("#F8FAFC")
    color_muted = colors.HexColor("#64748B")

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=28,
        rightMargin=28,
        topMargin=26,
        bottomMargin=28,
        title="Reporte de Actividad Diaria",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ActividadTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=15,
        textColor=color_primary,
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "ActividadMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        textColor=color_muted,
        leading=12,
    )
    foot_style = ParagraphStyle(
        "ActividadFoot",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        textColor=color_muted,
    )
    cell_style = ParagraphStyle(
        "ActividadCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9,
        textColor=colors.HexColor("#0F172A"),
    )
    amount_style = ParagraphStyle(
        "ActividadAmount",
        parent=cell_style,
        alignment=2,  # right
    )

    empresa_nombre = "TortugaTur"
    empresa_ruc = ""
    empresa_direccion = ""
    empresa_telefono = ""
    empresa_correo = ""
    if empresa is not None:
        empresa_nombre = getattr(empresa, "nombre_empresa", "") or empresa_nombre
        empresa_ruc = getattr(empresa, "ruc", "") or ""
        empresa_direccion = getattr(empresa, "direccion", "") or ""
        empresa_telefono = getattr(empresa, "telefono", "") or ""
        empresa_correo = getattr(empresa, "correo", "") or ""

    story = [
        Paragraph("REPORTE DE ACTIVIDAD DIARIA", title_style),
        Paragraph(f"Empresa: {_safe_text(empresa_nombre)}", meta_style),
        Paragraph(f"RUC: {_safe_text(empresa_ruc, 'No configurado')}", meta_style),
        Paragraph(_safe_text(titulo), meta_style),
        Paragraph(
            f"Fecha: {fecha.strftime('%d/%m/%Y')} | Registros: {int(resumen.get('total_registros', 0) or 0)}",
            meta_style,
        ),
        Paragraph(
            f"Direccion: {_safe_text(empresa_direccion)} | Telefono: {_safe_text(empresa_telefono)} | Correo: {_safe_text(empresa_correo)}",
            meta_style,
        ),
        Spacer(1, 10),
    ]

    data = [["Tipo", "Ref", "Usuario", "Detalle", "Estado", "Hora", "Monto"]]
    for item in items:
        dt = item.get("dt")
        hora = dt.strftime("%I:%M %p") if dt else "-"
        monto_raw = item.get("monto")
        monto = f"${float(monto_raw):.2f}" if monto_raw is not None else "-"
        ref = f"#{int(item.get('id', 0)):05d}" if item.get("id") else "-"
        detalle = f"{_safe_text(item.get('titulo', ''))} | {_safe_text(item.get('tour', ''))}"
        usuario = _safe_text(item.get("usuario", "-"))
        estado_raw = _safe_text(item.get("estado", "-"))
        estado_pretty = estado_raw.replace("_", " ")
        data.append([
            Paragraph(str(item.get("tipo", "")).upper(), cell_style),
            Paragraph(ref, cell_style),
            Paragraph(usuario, cell_style),
            Paragraph(detalle, cell_style),
            Paragraph(estado_pretty.upper(), cell_style),
            Paragraph(hora, cell_style),
            Paragraph(monto, amount_style),
        ])

    if len(data) == 1:
        data.append([
            Paragraph("-", cell_style),
            Paragraph("-", cell_style),
            Paragraph("-", cell_style),
            Paragraph("No hay actividad para esta fecha.", cell_style),
            Paragraph("-", cell_style),
            Paragraph("-", cell_style),
            Paragraph("-", amount_style),
        ])

    total_ventas = float(resumen.get("total_ventas", 0) or 0)
    data.append([
        "",
        "",
        "",
        "",
        "",
        Paragraph("TOTAL VENTAS", cell_style),
        Paragraph(f"${total_ventas:,.2f}", amount_style),
    ])

    table = Table(
        data,
        colWidths=[44, 50, 70, 160, 100, 56, 62],
        repeatRows=1,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), color_primary),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 8),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("GRID", (0, 0), (-1, -2), 0.5, color_border),
            ("ALIGN", (0, 0), (2, -1), "CENTER"),
            ("ALIGN", (3, 1), (4, -2), "LEFT"),
            ("ALIGN", (5, 0), (6, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTSIZE", (0, 1), (-1, -1), 7.5),
            ("FONTNAME", (5, -1), (6, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), color_light),
            ("LINEABOVE", (0, -1), (-1, -1), 1, color_primary),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F8FAFC")]),
        ])
    )
    story.append(table)
    story.append(Spacer(1, 8))
    story.append(Paragraph("Reporte generado desde el panel de gestion - TortugaTur.", foot_style))

    doc.build(story)
    buffer.seek(0)
    return buffer
