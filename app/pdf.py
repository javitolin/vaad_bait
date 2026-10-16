import os

from fpdf import FPDF

from ledger import METHOD_HE, date_he, money, period_he

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
A5 = (148, 210)


def _page() -> FPDF:
    pdf = FPDF(format=A5)
    pdf.set_margins(14, 14, 14)
    pdf.set_auto_page_break(False)
    pdf.add_font("Rubik", "", os.path.join(FONTS, "Rubik-Regular.ttf"))
    pdf.add_font("Rubik", "B", os.path.join(FONTS, "Rubik-Bold.ttf"))
    # Hebrew needs the bidi pass that only the shaping engine performs; without it
    # mixed Hebrew and numbers come out in the wrong order.
    pdf.set_text_shaping(use_shaping_engine=True, direction="rtl")
    pdf.add_page()
    return pdf


def _line(pdf: FPDF, label: str, value: str) -> None:
    pdf.set_font("Rubik", "B", 11)
    label_w = 34
    pdf.set_x(A5[0] - pdf.r_margin - label_w)
    pdf.cell(label_w, 9, label, align="R")
    pdf.set_font("Rubik", "", 11)
    pdf.set_x(pdf.l_margin)
    pdf.cell(A5[0] - pdf.l_margin - pdf.r_margin - label_w - 2, 9, value, align="R", new_x="LMARGIN", new_y="NEXT")


def receipt_pdf(r: dict, settings: dict) -> bytes:
    pdf = _page()
    width = A5[0] - pdf.l_margin - pdf.r_margin

    pdf.set_font("Rubik", "B", 15)
    pdf.cell(width, 8, settings["building_name"], align="R", new_x="LMARGIN", new_y="NEXT")
    if settings.get("building_address"):
        pdf.set_font("Rubik", "", 10)
        pdf.cell(width, 6, settings["building_address"], align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)
    pdf.set_font("Rubik", "B", 20)
    pdf.cell(width, 12, f"קבלה מס׳ {r['no']}", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(180, 180, 180)
    pdf.line(pdf.l_margin, pdf.get_y() + 2, A5[0] - pdf.r_margin, pdf.get_y() + 2)
    pdf.ln(8)

    method = METHOD_HE[r["method"]]
    if r["method"] == "cheque":
        method += f" מס׳ {r['cheque_no']}"
        if r.get("cheque_due"):
            method += f", לפירעון {date_he(r['cheque_due'])}"

    _line(pdf, "תאריך:", date_he(r["issued_at"]))
    _line(pdf, "התקבל מאת:", r["payer"] or "—")
    _line(pdf, "דירה:", str(r["apt_id"]))
    _line(pdf, "סכום:", f"₪{money(r['amount'])}")
    _line(pdf, "עבור:", f"דמי ועד בית, {period_he(r['period'])}")
    _line(pdf, "אמצעי תשלום:", method)
    if r.get("note"):
        _line(pdf, "הערה:", r["note"])

    if settings.get("treasurer_name"):
        pdf.set_y(-36)
        pdf.set_font("Rubik", "", 11)
        pdf.cell(width, 7, f"גזבר: {settings['treasurer_name']}", align="R", new_x="LMARGIN", new_y="NEXT")

    if r.get("voided_at"):
        pdf.set_text_color(200, 30, 30)
        pdf.set_font("Rubik", "B", 54)
        with pdf.rotation(25, A5[0] / 2, A5[1] / 2):
            pdf.set_xy(0, A5[1] / 2 - 15)
            pdf.cell(A5[0], 30, "מבוטלת", align="C")
        pdf.set_font("Rubik", "", 10)
        pdf.set_xy(pdf.l_margin, A5[1] / 2 + 30)
        pdf.cell(width, 6, f"בוטלה {date_he(r['voided_at'])}: {r['void_reason']}", align="C")
        pdf.set_text_color(0, 0, 0)

    pdf.set_y(-20)
    pdf.set_font("Rubik", "", 8)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(width, 5, "הופק במערכת ועד הבית", align="C")
    return bytes(pdf.output())
