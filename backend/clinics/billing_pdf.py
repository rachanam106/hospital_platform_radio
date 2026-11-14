"""Private printable invoice, receipt, and refund documents."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _txt(value):
    return escape(str(value or "—")).replace("\r\n", "\n").replace("\n", "<br/>")


def _money(value, currency):
    return f"{currency} {value / 100:,.2f}"


def _base(title, clinic, patient, invoice_number, on_date):
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=20 * mm, bottomMargin=20 * mm, title=title)
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="BillClinic", parent=styles["Title"], textColor=colors.HexColor("#18334e"), fontSize=17, leading=21))
    styles.add(ParagraphStyle(name="BillSmall", parent=styles["BodyText"], fontSize=8, leading=11, textColor=colors.HexColor("#5c6b7a")))
    story = [Paragraph(_txt(clinic.get("name") or "Clinic"), styles["BillClinic"])]
    contact = [clinic.get(k) for k in ("address", "phone", "email") if clinic.get(k)]
    if contact:
        story.append(Paragraph(" · ".join(_txt(v) for v in contact), styles["BillSmall"]))
    story.extend([Spacer(1, 7 * mm), Paragraph(_txt(title), styles["Heading1"])])
    meta = [
        ["Invoice", _txt(invoice_number), "Date", _txt(on_date)],
        ["Patient", _txt(patient.get("name")), "MRN", _txt(patient.get("medical_record_number"))],
        ["Date of birth", _txt(patient.get("date_of_birth")), "Examination", _txt(patient.get("examination") or "—")],
    ]
    table = Table(meta, colWidths=[25 * mm, 58 * mm, 26 * mm, 55 * mm])
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f3f6")), ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f0f3f6")), ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#d6dde4")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story.extend([Spacer(1, 5 * mm), table, Spacer(1, 4 * mm)])
    return buffer, doc, styles, story


def _line_table(invoice, styles):
    data = [["Service", "Qty", "Unit price", "Discount", "Tax", "Line total"]]
    for line in invoice.line_items.all():
        net = line.subtotal_minor - line.discount_minor + line.tax_minor
        data.append([Paragraph(_txt(line.description), styles["BodyText"]), str(line.quantity), _money(line.unit_price_minor, invoice.currency), _money(line.discount_minor, invoice.currency), _money(line.tax_minor, invoice.currency), _money(net, invoice.currency)])
    table = Table(data, colWidths=[56 * mm, 11 * mm, 28 * mm, 24 * mm, 22 * mm, 28 * mm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9eff5")), ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#d6dde4")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("FONTSIZE", (0, 0), (-1, -1), 7.5), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return table


def build_invoice_pdf(invoice):
    clinic, patient = invoice.clinic_context, invoice.patient_context
    if invoice.examination_id:
        patient = {**patient, "examination": invoice.examination.procedure_name}
    buffer, doc, styles, story = _base("PATIENT INVOICE", clinic, patient, invoice.invoice_number, invoice.invoice_date.isoformat())
    story.extend([_line_table(invoice, styles), Spacer(1, 5 * mm)])
    totals = [["Gross", _money(invoice.gross_amount_minor, invoice.currency)], ["Discount", _money(invoice.discount_minor, invoice.currency)], ["Tax", _money(invoice.tax_minor, invoice.currency)], ["Amount payable", _money(invoice.amount_minor, invoice.currency)], ["Paid", _money(invoice.paid_minor, invoice.currency)], ["Outstanding", _money(invoice.balance_minor, invoice.currency)]]
    table = Table(totals, colWidths=[45 * mm, 45 * mm], hAlign="RIGHT")
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#d6dde4")), ("FONTNAME", (0, -3), (-1, -3), "Helvetica-Bold"), ("BACKGROUND", (0, -3), (-1, -3), colors.HexColor("#e9eff5")), ("FONTSIZE", (0, 0), (-1, -1), 9), ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("PADDING", (0, 0), (-1, -1), 6)]))
    story.extend([table, Spacer(1, 5 * mm), Paragraph(f"Status: {_txt(invoice.status)}", styles["BodyText"])])
    doc.build(story)
    return buffer.getvalue()


def build_payment_receipt_pdf(payment):
    invoice = payment.invoice
    patient = invoice.patient_context
    if invoice.examination_id:
        patient = {**patient, "examination": invoice.examination.procedure_name}
    receipt_no = f"RCT-{invoice.invoice_number}-{str(payment.pk)[:8].upper()}"
    buffer, doc, styles, story = _base("PAYMENT RECEIPT", invoice.clinic_context, patient, invoice.invoice_number, payment.created_at.date().isoformat())
    story.extend([Paragraph(f"Receipt number: {_txt(receipt_no)}", styles["BodyText"]), Spacer(1, 4 * mm), _line_table(invoice, styles), Spacer(1, 5 * mm)])
    data = [["Payment amount", _money(payment.amount_minor, payment.currency)], ["Method", _txt(payment.method.upper())], ["Reference", _txt(payment.reference)], ["Received by", _txt(payment.recorded_by.get_full_name())], ["Receipt state", "Staff-confirmed; not externally verified" if payment.confirmed_received else "Recorded by staff; not externally verified"]]
    table = Table(data, colWidths=[48 * mm, 90 * mm])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#d6dde4")), ("FONTSIZE", (0, 0), (-1, -1), 9), ("PADDING", (0, 0), (-1, -1), 6)]))
    story.append(table)
    doc.build(story)
    return buffer.getvalue()


def build_refund_receipt_pdf(refund):
    payment = refund.payment
    invoice = payment.invoice
    patient = invoice.patient_context
    if invoice.examination_id:
        patient = {**patient, "examination": invoice.examination.procedure_name}
    buffer, doc, styles, story = _base("REFUND RECEIPT", invoice.clinic_context, patient, invoice.invoice_number, refund.created_at.date().isoformat())
    data = [["Refund number", f"REF-{str(refund.pk)[:12].upper()}"], ["Refund amount", _money(refund.amount_minor, payment.currency)], ["Original payment method", payment.method.upper()], ["Reason", refund.reason], ["Recorded by", refund.recorded_by.get_full_name()]]
    table = Table(data, colWidths=[48 * mm, 115 * mm])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#d6dde4")), ("FONTSIZE", (0, 0), (-1, -1), 9), ("PADDING", (0, 0), (-1, -1), 6)]))
    story.append(table)
    doc.build(story)
    return buffer.getvalue()
