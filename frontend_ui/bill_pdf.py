"""Builds a printable A5 PDF for a bill (reportlab)."""
import os
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SHOP_NAME = os.getenv("SHOP_NAME", "Grain Store")
SHOP_ADDRESS = os.getenv("SHOP_ADDRESS", "")
SHOP_PHONE = os.getenv("SHOP_PHONE", "")


def rs(x):
    # The built-in PDF fonts have no rupee glyph, so "Rs." is used on printed bills.
    return f"Rs. {float(x):,.2f}"


def build_bill_pdf(bill: dict) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A5, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Bill {bill.get('bill_no', '')}")
    base = getSampleStyleSheet()["Normal"]
    shop = ParagraphStyle("shop", parent=base, fontName="Helvetica-Bold", fontSize=16, alignment=TA_CENTER, spaceAfter=2)
    small = ParagraphStyle("small", parent=base, fontSize=8.5, alignment=TA_CENTER, textColor=colors.grey)
    cell = ParagraphStyle("cell", parent=base, fontSize=9, leading=11)
    meta = ParagraphStyle("meta", parent=base, fontSize=9, leading=12)

    story = [Paragraph(escape(SHOP_NAME), shop)]
    if SHOP_ADDRESS:
        story.append(Paragraph(escape(SHOP_ADDRESS), small))
    if SHOP_PHONE:
        story.append(Paragraph(escape(f"Phone: {SHOP_PHONE}"), small))
    story += [Spacer(1, 4 * mm), HRFlowable(width="100%", color=colors.lightgrey), Spacer(1, 2 * mm)]
    if bill.get("status") == "voided":
        story += [Paragraph("*** THIS BILL HAS BEEN VOIDED ***", ParagraphStyle("void", parent=base, fontName="Helvetica-Bold", fontSize=11,
                                                                               alignment=TA_CENTER, textColor=colors.HexColor("#c62828"))), Spacer(1, 2 * mm)]

    customer = bill.get("customer_name") or "Walk-in customer"
    if bill.get("customer_phone"):
        customer += f" ({bill['customer_phone']})"
    info = [
        [Paragraph(f"<b>Bill No:</b> {escape(str(bill.get('bill_no', '')))}", meta),
         Paragraph(f"<b>Date:</b> {escape(str(bill.get('odate', '')))}", meta)],
        [Paragraph(f"<b>Customer:</b> {escape(customer)}", meta),
         Paragraph(f"<b>Cashier:</b> {escape(str(bill.get('cashier_name') or '-'))}", meta)],
    ]
    t = Table(info, colWidths=[62 * mm, 62 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    story += [t, Spacer(1, 3 * mm)]

    rows = [["#", "Item", "Qty", "Rate", "Amount"]]
    for i, it in enumerate(bill.get("items", []), 1):
        rows.append([str(i), Paragraph(escape(it["pname"]), cell), str(it["qty"]), rs(it["unit_price"]), rs(it["line_total"])])
    items = Table(rows, colWidths=[8 * mm, 56 * mm, 11 * mm, 24 * mm, 25 * mm], repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2e7d32")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7f4")]),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, colors.lightgrey),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [items, Spacer(1, 3 * mm)]

    totals = [["Subtotal", rs(bill["subtotal"])]]
    if float(bill.get("discount") or 0) > 0:
        label = f"Discount ({bill['offer_name']})" if bill.get("offer_name") else "Discount"
        totals.append([label, "- " + rs(bill["discount"])])
    totals.append(["TOTAL", rs(bill["total_amt"])])
    totals.append([f"Paid by {bill.get('payment_method', '')}", rs(bill["amount_received"]) if bill.get("amount_received") is not None else rs(bill["total_amt"])])
    if bill.get("change_due") not in (None, 0) and float(bill["change_due"]) > 0:
        totals.append(["Change returned", rs(bill["change_due"])])
    tt = Table(totals, colWidths=[99 * mm, 25 * mm], hAlign="RIGHT")
    total_row = [r[0] for r in totals].index("TOTAL")
    tt.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, total_row), (-1, total_row), "Helvetica-Bold"), ("FONTSIZE", (0, total_row), (-1, total_row), 12),
        ("LINEABOVE", (0, total_row), (-1, total_row), 0.8, colors.black),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [tt, Spacer(1, 8 * mm), Paragraph("Thank you for shopping with us!", ParagraphStyle("ty", parent=small, fontSize=10, textColor=colors.black))]
    doc.build(story)
    return buf.getvalue()
