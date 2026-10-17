import os

from fpdf import FPDF
from fpdf.fonts import FontFace

from ledger import ACCOUNTS, METHOD_HE, METHOD_SHORT, date_he, money, period_he

FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
A5 = (148, 210)
A4 = (210, 297)
GREY = (110, 120, 130)


def _page(size: tuple = A5) -> FPDF:
    pdf = FPDF(format=size)
    pdf.set_margins(14, 14, 14)
    # Long A4 reports flow onto more pages and stop short of the footer; a receipt is one fixed page.
    pdf.set_auto_page_break(size != A5, margin=26)
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


def receipt_pdf(r: dict, settings: dict, location: str) -> bytes:
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

    pdf.set_y(-22)
    pdf.set_font("Rubik", "", 8)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(width, 5, "הופק במערכת ועד הבית. העותק השמור:", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Rubik", "", 6.5)
    pdf.cell(width, 4, location, align="C")
    return bytes(pdf.output())


def _owed(agorot: int) -> str:
    """Balances in words, so no minus sign has to survive right-to-left layout."""
    if agorot > 0:
        return f"חוב ₪{money(agorot)}"
    if agorot < 0:
        return f"זכות ₪{money(-agorot)}"
    return "מאוזן"


def _heading(pdf: FPDF, title: str, subtitle: str, settings: dict) -> None:
    width = pdf.epw
    pdf.set_font("Rubik", "B", 11)
    pdf.set_text_color(*GREY)
    pdf.cell(width, 6, " · ".join(x for x in (settings["building_name"], settings.get("building_address")) if x),
             align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Rubik", "B", 20)
    pdf.cell(width, 12, title, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Rubik", "", 11)
    pdf.cell(width, 6, subtitle, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)


def _section(pdf: FPDF, title: str) -> None:
    # A heading needs room for at least a few rows under it, or it moves to the next page.
    if pdf.get_y() > pdf.h - 70:
        pdf.add_page()
    pdf.set_font("Rubik", "B", 13)
    pdf.cell(pdf.epw, 9, title, align="R", new_x="LMARGIN", new_y="NEXT")


def _table(pdf: FPDF, rows: list[list[str]], widths: tuple, numeric: set[int] = frozenset(), bold_last: bool = False) -> None:
    # fpdf2 lays a table out left to right; Hebrew reads right to left, so the columns go in reversed.
    order = list(reversed(range(len(widths))))
    align = tuple("LEFT" if i in numeric else "RIGHT" for i in order)
    pdf.set_font("Rubik", "", 9.5)
    with pdf.table(col_widths=tuple(widths[i] for i in order), text_align=align, line_height=7,
                   borders_layout="HORIZONTAL_LINES", headings_style=FontFace(emphasis="BOLD", fill_color=(236, 244, 242)),
                   padding=(1, 2)) as table:
        for n, values in enumerate(rows):
            style = FontFace(emphasis="BOLD") if bold_last and n == len(rows) - 1 else None
            row = table.row(style=style)
            for i in order:
                row.cell(values[i])
    pdf.ln(5)


def _footer(pdf: FPDF, generated: str, location: str = "") -> None:
    pdf.set_auto_page_break(False)
    pdf.set_y(-18)
    pdf.set_font("Rubik", "", 8)
    pdf.set_text_color(*GREY)
    pdf.cell(pdf.epw, 4, f"הופק במערכת ועד הבית, {generated}" + (". העותק השמור:" if location else ""),
             align="C", new_x="LMARGIN", new_y="NEXT")
    if location:
        pdf.set_font("Rubik", "", 6.5)
        pdf.cell(pdf.epw, 4, location, align="C")
    pdf.set_text_color(0, 0, 0)


def statement_pdf(st: dict, settings: dict, generated: str) -> bytes:
    pdf = _page(A4)
    _heading(pdf, f"אישור תשלומים — דירה {st['apartment']['id']}",
             f"{st['name'] or ''}  ·  שנת {st['year']}", settings)
    rows = [["חודש", "חיוב", "שולם", "אמצעי תשלום", "קבלה", "שולם ע״י"]]
    for m in st["months"]:
        if not m["payments"]:
            rows.append([period_he(m["period"]), money(m["due"]) if m["due"] else "—", "—", "", "", ""])
            continue
        for n, p in enumerate(m["payments"]):
            number = p["cheque_no"] if p["cheque_no"] not in (None, "—") else ""
            method = f"{METHOD_SHORT[p['method']]} {number}".strip()
            if p["status"] == "held":
                receipt, paid = "התקבל, טרם הופקה", "—"
            else:
                receipt, paid = (str(p["receipt"]) if p["receipt"] else "ידנית"), money(p["amount"])
            rows.append([period_he(m["period"]) if n == 0 else "", money(m["due"]) if n == 0 and m["due"] else "",
                         paid, method, receipt, p["payer"] or ""])
    rows.append(["סה״כ", money(st["due"]), money(st["paid"]), "", "", ""])
    _table(pdf, rows, (24, 15, 15, 20, 18, 30), numeric={1, 2}, bold_last=True)
    pdf.set_font("Rubik", "B", 12)
    pdf.cell(pdf.epw, 8, f"יתרת הדירה נכון ל{generated}: {_owed(st['balance'])}", align="R",
             new_x="LMARGIN", new_y="NEXT")
    if settings.get("treasurer_name"):
        pdf.set_font("Rubik", "", 10)
        pdf.cell(pdf.epw, 7, f"גזבר: {settings['treasurer_name']}", align="R")
    _footer(pdf, generated)
    return bytes(pdf.output())


def summary_pdf(sm: dict, settings: dict, generated: str, location: str) -> bytes:
    pdf = _page(A4)
    _heading(pdf, f"סיכום שנתי {sm['year']}", f"הופק {generated}", settings)

    pdf.set_font("Rubik", "B", 12)
    lines = [("הכנסות מדמי ועד", f"₪{money(sm['income'])}")]
    if sm["other_income"]:
        lines.append(("הכנסות אחרות", f"₪{money(sm['other_income'])}"))
    for label, value in (*lines,
                         ("הוצאות", f"₪{money(sm['spent'])}"),
                         ("עודף" if sm["net"] >= 0 else "גירעון", f"₪{money(abs(sm['net']))}")):
        pdf.cell(pdf.epw, 8, f"{label}: {value}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    _section(pdf, "הכנסות לפי דירה")
    rows = [["דירה", "שם", "חודשים ששולמו", "חויב", "שולם"]]
    rows += [[str(r["apt"]), r["name"] or "", str(r["months"]), money(r["due"]), money(r["paid"])] for r in sm["apartments"]]
    rows.append(["סה״כ", "", "", money(sum(r["due"] for r in sm["apartments"])), money(sm["income"])])
    _table(pdf, rows, (10, 40, 22, 20, 20), numeric={3, 4}, bold_last=True)

    if sm["other"]:
        _section(pdf, "הכנסות אחרות")
        rows = [["תיאור", "סכום"]] + [[o["description"], money(o["amount"])] for o in sm["other"]]
        _table(pdf, rows, (60, 25), numeric={1})

    _section(pdf, "הוצאות לפי קטגוריה")
    if sm["expenses"]:
        rows = [["קטגוריה", "סכום"]] + [[c["category"], money(c["amount"])] for c in sm["expenses"]]
        rows.append(["סה״כ", money(sm["spent"])])
        _table(pdf, rows, (60, 25), numeric={1}, bold_last=True)
    else:
        pdf.set_font("Rubik", "", 10)
        pdf.cell(pdf.epw, 7, "לא נרשמו הוצאות.", align="R", new_x="LMARGIN", new_y="NEXT")

    if sm["accounts"]:
        _section(pdf, f"יתרות לסוף {sm['year']}")
        pdf.set_font("Rubik", "", 11)
        bank, treasurer = sm["accounts"]["bank"], sm["accounts"]["treasurer"]
        pdf.cell(pdf.epw, 7, f"{ACCOUNTS['bank']}: ₪{money(bank)}", align="R", new_x="LMARGIN", new_y="NEXT")
        owes = f"הוועד חייב לגזבר ₪{money(-treasurer)}" if treasurer < 0 else f"₪{money(treasurer)} בידי הגזבר"
        pdf.cell(pdf.epw, 7, f"{ACCOUNTS['treasurer']}: {owes}", align="R", new_x="LMARGIN", new_y="NEXT")
    _footer(pdf, generated, location)
    return bytes(pdf.output())
