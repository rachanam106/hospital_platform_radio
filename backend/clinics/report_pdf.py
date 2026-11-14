"""PDF representation of a signed report snapshot; never renders live draft inputs."""
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .reporting import REPORT_TEMPLATES


def _text(value):
    return escape(str(value or "")).replace("\r\n", "\n").replace("\n", "<br/>") or "—"


def build_signed_report_pdf(report):
    """Render only persisted signed fields and the context frozen at signature."""
    if not report.signed_context or report.status == "draft":
        raise ValueError("A signed report snapshot is required for PDF export.")

    snapshot = report.signed_context
    clinic = snapshot.get("clinic", {})
    patient = snapshot.get("patient", {})
    exam = snapshot.get("examination", {})
    radiologist = snapshot.get("radiologist", {})
    report_date = snapshot.get("report_date", "")
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=19 * mm, leftMargin=19 * mm,
        topMargin=24 * mm, bottomMargin=20 * mm,
        title=f"Radiology report v{report.version}",
        author=radiologist.get("name") or "",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ClinicTitle", parent=styles["Title"], fontSize=16, leading=20, textColor=colors.HexColor("#1f3550"), alignment=TA_CENTER, spaceAfter=6))
    styles.add(ParagraphStyle(name="SectionHeading", parent=styles["Heading2"], fontSize=10, leading=13, textColor=colors.HexColor("#315d9d"), spaceBefore=12, spaceAfter=5, keepWithNext=True))
    styles.add(ParagraphStyle(name="BodyClinical", parent=styles["BodyText"], fontSize=9, leading=13, textColor=colors.HexColor("#263445"), splitLongWords=True))
    styles.add(ParagraphStyle(name="SmallMeta", parent=styles["BodyText"], fontSize=8, leading=11, textColor=colors.HexColor("#596779")))

    contact = [value for value in (clinic.get("address"), clinic.get("phone"), clinic.get("email")) if value]
    story = [Paragraph(_text(clinic.get("name") or "Clinic name not configured"), styles["ClinicTitle"])]
    if contact:
        story.append(Paragraph(" · ".join(_text(item) for item in contact), styles["SmallMeta"]))
    story.extend([Spacer(1, 7 * mm), Paragraph("RADIOLOGY REPORT", styles["Heading1"])])

    rows = [
        ["Patient", _text(patient.get("name")), "MRN", _text(patient.get("medical_record_number"))],
        ["Date of birth", _text(patient.get("date_of_birth")), "Examination", _text(exam.get("name"))],
        ["Modality", _text(exam.get("modality")), "Report date", _text(report_date)],
        ["Version / status", f"v{report.version} · {_text(report.status)}", "Template", _text(report.template)],
    ]
    table = Table(rows, colWidths=[25 * mm, 57 * mm, 25 * mm, 55 * mm], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1f4f8")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f1f4f8")),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#263445")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LEADING", (0, 0), (-1, -1), 11),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOX", (0, 0), (-1, -1), .5, colors.HexColor("#d9e0e8")),
        ("INNERGRID", (0, 0), (-1, -1), .35, colors.HexColor("#d9e0e8")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.extend([table, Spacer(1, 4 * mm)])

    story.extend([Paragraph("Clinical indication", styles["SectionHeading"]), Paragraph(_text(exam.get("clinical_indication")), styles["BodyClinical"])])
    values = {
        "technique": report.technique,
        "sequences": report.sequences,
        "comparison": report.comparison,
        "findings": report.findings,
        "impression": report.impression,
        "additional_comments": report.additional_comments,
    }
    labels = {
        "technique": "Technique", "sequences": "Sequences", "comparison": "Comparison",
        "findings": "Findings", "impression": "Impression", "additional_comments": "Additional comments",
    }
    sections = [(labels[field], values[field]) for field in REPORT_TEMPLATES[report.template]["fields"]]
    for title, value in sections:
        story.extend([Paragraph(_text(title), styles["SectionHeading"]), Paragraph(_text(value), styles["BodyClinical"])])

    radiologist_name = radiologist.get("name") or "Name not provided"
    registration = radiologist.get("registration_number")
    signoff = f"Reporting radiologist: {_text(radiologist_name)}"
    if registration:
        signoff += f" · Registration: {_text(registration)}"
    signoff += f" · Signed: {_text(report.signed_at.isoformat() if report.signed_at else report_date)}"
    story.extend([Spacer(1, 8 * mm), Paragraph(signoff, styles["SmallMeta"])])

    def decorate(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#d9e0e8"))
        canvas.line(19 * mm, 15 * mm, A4[0] - 19 * mm, 15 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#778395"))
        canvas.drawString(19 * mm, 10 * mm, f"{clinic.get('name') or 'Clinic'} · Report v{report.version} · {_text(report.status)}")
        canvas.drawRightString(A4[0] - 19 * mm, 10 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buffer.getvalue()
