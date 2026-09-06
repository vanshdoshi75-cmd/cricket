"""
receipt.py
Generates a downloadable PDF booking receipt using reportlab.
Built entirely in-memory (BytesIO) — nothing is written to disk.
"""

import io
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
)

GREEN = colors.HexColor("#1f7a3f")
GREY = colors.HexColor("#666666")
LIGHT_GREY = colors.HexColor("#f2f2f2")


def _status_label(booking):
    if booking["status"] == "cancelled":
        return "CANCELLED", colors.HexColor("#c0392b")
    if booking["payment_status"] in ("paid", "advance_paid"):
        return "PAID", GREEN
    return "PAYMENT PENDING", colors.HexColor("#b8860b")


def generate_receipt_pdf(booking, turf):
    """Return a BytesIO containing a one-page PDF receipt for `booking`."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=20 * mm, bottomMargin=20 * mm,
        leftMargin=20 * mm, rightMargin=20 * mm,
        title=f"Receipt {booking['booking_code']}",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TurfTitle", parent=styles["Title"], fontSize=18, textColor=GREEN,
        spaceAfter=2,
    )
    muted_style = ParagraphStyle(
        "Muted", parent=styles["Normal"], fontSize=9, textColor=GREY,
    )
    section_style = ParagraphStyle(
        "Section", parent=styles["Heading3"], fontSize=11, spaceBefore=14,
        spaceAfter=6, textColor=colors.HexColor("#222222"),
    )
    status_style = ParagraphStyle(
        "Status", parent=styles["Normal"], fontSize=13, alignment=TA_RIGHT,
    )

    story = []

    # --- Header ---
    story.append(Paragraph(turf["name"] or "Turf Booking Receipt", title_style))
    header_bits = [turf["address"], turf["contact_number"]]
    story.append(Paragraph(" &middot; ".join(b for b in header_bits if b), muted_style))
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#dddddd"), thickness=1))
    story.append(Spacer(1, 10))

    # --- Receipt title row + status ---
    label, color = _status_label(booking)
    status_style_colored = ParagraphStyle(
        "StatusColored", parent=status_style, textColor=color,
    )
    top_row = Table(
        [[
            Paragraph(f"<b>Booking Receipt</b><br/><font size=9 color='#666666'>"
                      f"Booking Code: {booking['booking_code']}</font>", styles["Normal"]),
            Paragraph(f"<b>{label}</b>", status_style_colored),
        ]],
        colWidths=[110 * mm, 50 * mm],
    )
    top_row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(top_row)

    # --- Customer & booking details ---
    story.append(Paragraph("Booking Details", section_style))
    details = [
        ["Customer Name", booking["customer_name"]],
        ["Mobile", booking["mobile"]],
        ["Email", booking["email"] or "—"],
        ["Date", f"{booking['day_name']}, {booking['booking_date']}"],
        ["Time Slot", f"{booking['start_time']} – {booking['end_time']}"],
        ["Duration", f"{booking['hours']} hour(s)"],
    ]
    if booking["players"]:
        details.append(["Players", str(booking["players"])])
    detail_table = Table(details, colWidths=[45 * mm, 115 * mm])
    detail_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), GREY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(detail_table)

    # --- Payment details ---
    story.append(Paragraph("Payment Details", section_style))
    payment_rows = [
        ["Payment Method", "Pay Online" if booking["payment_method"] == "online" else "Pay at Turf"],
        ["Total Amount", f"Rs. {booking['total_amount']}"],
        ["Amount Paid", f"Rs. {booking['amount_paid']}"],
        ["Remaining (payable at turf)", f"Rs. {booking['remaining_amount']}"],
    ]
    if booking["transaction_id"]:
        payment_rows.append(["Transaction / Reference ID", booking["transaction_id"]])
    if booking["paid_at"]:
        payment_rows.append(["Paid At", booking["paid_at"]])

    payment_table = Table(payment_rows, colWidths=[65 * mm, 95 * mm])
    payment_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (0, -1), GREY),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 1), (-1, 1), 0.5, colors.HexColor("#dddddd")),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
    ]))
    story.append(payment_table)

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#dddddd"), thickness=1))
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        f"Generated on {datetime.now().strftime('%d %b %Y, %I:%M %p')}. "
        f"This is a system-generated receipt and does not require a signature.",
        muted_style,
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer
