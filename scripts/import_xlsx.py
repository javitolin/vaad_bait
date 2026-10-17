"""Load apartments and 2026 payments from the old "תשלומי ועד" spreadsheet.

    .venv/bin/python -I scripts/import_xlsx.py sheet.xlsx                 # local .devdata
    BUCKET=<bucket> .venv/bin/python -I scripts/import_xlsx.py sheet.xlsx  # the live database

Months marked "קיבל קבלה" already got a paper receipt, so they become paid
payments with no new receipt number. Cheques without a receipt yet (Nov-Dec)
and transfers without a receipt yet (Nov-Dec) become marks, processed on the לטיפול page. Refuses to touch a database
that already has payments.
"""
import os
import re
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import boto3  # noqa: E402
import openpyxl  # noqa: E402

import db  # noqa: E402
import ledger  # noqa: E402
from store import Store  # noqa: E402

YEAR = 2026
METHODS = {"צ׳ק": "cheque", "צ'ק": "cheque", "העברה": "transfer", "מזומן": "cash", "החזר חוב": "offset"}
UNKNOWN_CHEQUE = "—"  # paper-era cheques that were deposited without the number being recorded


def parse(text: str) -> tuple[str, bool] | None:
    """'צ׳ק, קיבל קבלה' -> ('cheque', True); 'צ׳ק' -> ('cheque', False)."""
    text = (text or "").strip()
    if not text:
        return None
    method = next((m for word, m in METHODS.items() if word in text), None)
    if method is None:
        raise SystemExit(f"unrecognised cell: {text!r}")
    # A cheque handed over in another month was still paid; the sheet tracks it as settled.
    settled = "קיבל קבלה" in text or "בחודש אחר" in text
    return method, settled


# Expense rows in the sheet, and the account each was paid from (the gardener in cash, from the treasurer).
EXPENSE_ROWS = range(20, 27)
CASH_CATEGORIES = {"גנן"}


def amount(value) -> int:
    """Cells hold numbers or simple formulas like '=230+650'."""
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return round(value * 100)
    text = str(value).lstrip("=")
    if not re.fullmatch(r"[\d.+*\s]+", text):
        raise SystemExit(f"unexpected expense cell: {value!r}")
    return sum(round(eval_product(term) * 100) for term in text.split("+"))


def eval_product(term: str) -> float:
    result = 1.0
    for factor in term.split("*"):
        result *= float(factor)
    return result


def expenses(path: str, through: str):
    sheet = openpyxl.load_workbook(path).active
    for r in EXPENSE_ROWS:
        category = (sheet.cell(r, 1).value or "").strip()
        if category.startswith("שונות"):
            category = "שונות"
        for month in range(1, 13):
            value = amount(sheet.cell(r, month + 2).value)
            if value and f"{YEAR}-{month:02d}" <= through:
                yield category, date(YEAR, month, 1), value


def rows(path: str):
    sheet = openpyxl.load_workbook(path, data_only=True).active
    for r in range(2, 14):
        apt = sheet.cell(r, 1).value
        name = " / ".join(line.strip() for line in str(sheet.cell(r, 2).value or "").splitlines() if line.strip())
        months = [parse(sheet.cell(r, c).value) for c in range(3, 15)]
        yield int(apt), name, months


def main(path: str) -> None:
    bucket = os.environ.get("BUCKET")
    # The live database must not be cached over the local playground copy.
    root = os.environ.get("DATA_DIR") or (tempfile.mkdtemp() if bucket else ".devdata")
    store = Store(root, boto3.client("s3") if bucket else None, bucket)
    with store.write() as conn:
        if conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]:
            raise SystemExit("database already has payments; not importing over them")
        db.save_settings(conn, {"start_period": f"{YEAR}-01"})
        paid = held = 0
        for apt, name, months in rows(path):
            latest = next((m[0] for m in reversed(months) if m), "cheque")
            conn.execute("UPDATE apartments SET occupant = ?, default_method = ? WHERE id = ?", (name, latest, apt))
            for month, entry in enumerate(months, start=1):
                if not entry:
                    continue
                method, settled = entry
                period = f"{YEAR}-{month:02d}"
                if settled:
                    status, note = "cleared", "קבלה ידנית (פנקס)"
                    paid += 1
                else:
                    status, note = "held", "יובא מהאקסל"
                    held += 1
                conn.execute(
                    "INSERT INTO payments (apt_id, date, amount, method, account, period, cheque_no, status, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    # Already-paid future months were paid by now, not on the 1st of that month.
                    (apt, min(date(YEAR, month, 1), date.today()).isoformat(), 25000, method, ledger.ACCOUNT[method], period,
                     UNKNOWN_CHEQUE if method == "cheque" and status == "cleared" else None,
                     status, note),
                )
        through = date.today().strftime("%Y-%m")
        spent = 0
        for category, day, value in expenses(path, through):
            ledger.add_expense(conn, day=day, vendor="", category=category, amount=value,
                               account="treasurer" if category in CASH_CATEGORIES else "bank", note="יובא מהאקסל")
            spent += 1
        ledger.ensure_dues(conn, through)
    print(f"imported 12 apartments, {paid} paid months, {held} marked months, {spent} expenses -> {bucket or store.path}")


if __name__ == "__main__":
    main(sys.argv[1])
