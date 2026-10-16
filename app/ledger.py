import calendar
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import db

FIRST_RECEIPT = 1000

ACCOUNT = {"transfer": "bank", "cheque": "bank", "cash": "treasurer", "bit": "treasurer", "offset": "treasurer"}
METHOD_HE = {"transfer": "העברה בנקאית", "cheque": "צ׳ק", "cash": "מזומן", "bit": "ביט", "offset": "קיזוז"}
METHOD_SHORT = {**METHOD_HE, "transfer": "העברה"}
MONTHS_HE = ("ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
             "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר")
PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class Invalid(ValueError):
    """Input the treasurer has to fix; the message is shown as is."""


def parse_money(text: str) -> int:
    try:
        value = Decimal(str(text).replace(",", "").replace("₪", "").strip() or "0")
    except InvalidOperation:
        raise Invalid(f"סכום לא תקין: {text}") from None
    if value != value.quantize(Decimal("0.01")):
        raise Invalid(f"סכום עם יותר משתי ספרות אחרי הנקודה: {text}")
    return int(value * 100)


def money(agorot: int) -> str:
    sign = "-" if agorot < 0 else ""
    whole, cents = divmod(abs(agorot), 100)
    return f"{sign}{whole:,}.{cents:02d}"


def check_period(period: str) -> str:
    if not PERIOD.match(period or ""):
        raise Invalid(f"חודש לא תקין: {period}")
    return period


def period_add(period: str, months: int) -> str:
    year, month = divmod(int(period[:4]) * 12 + int(period[5:]) - 1 + months, 12)
    return f"{year:04d}-{month + 1:02d}"


def date_add_months(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 + months, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def period_he(period: str) -> str:
    return f"{MONTHS_HE[int(period[5:]) - 1]} {period[:4]}"


def date_he(iso: str) -> str:
    return date.fromisoformat(iso[:10]).strftime("%d/%m/%Y")


def fill(template: str, values: dict) -> str:
    """Replace known {placeholders}; anything else is left exactly as typed."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m[1], m[0])), template)


def wa_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return "972" + digits[1:] if digits.startswith("0") else digits


# --- dues -------------------------------------------------------------------

def missing_dues(conn: sqlite3.Connection, through: str) -> list[tuple[int, str]]:
    start = db.settings(conn).get("start_period") or through
    have = {tuple(row) for row in conn.execute("SELECT apt_id, period FROM charges WHERE kind = 'dues'")}
    apartments = [row[0] for row in conn.execute("SELECT id FROM apartments")]
    missing, period = [], start
    while period <= through:
        missing += [(apt, period) for apt in apartments if (apt, period) not in have]
        period = period_add(period, 1)
    return missing


def ensure_dues(conn: sqlite3.Connection, through: str) -> None:
    conn.executemany(
        "INSERT INTO charges (apt_id, period, kind, amount) "
        "SELECT id, ?, 'dues', monthly_fee FROM apartments WHERE id = ?",
        [(period, apt) for apt, period in missing_dues(conn, through)],
    )


def balances(conn: sqlite3.Connection) -> dict[int, int]:
    """Positive = the apartment owes the ועד."""
    return dict(conn.execute("""
        SELECT a.id,
               COALESCE((SELECT SUM(amount) FROM charges c WHERE c.apt_id = a.id), 0)
             - COALESCE((SELECT SUM(amount) FROM payments p WHERE p.apt_id = a.id AND p.status = 'cleared'), 0)
        FROM apartments a
    """).fetchall())


def grid(conn: sqlite3.Connection, year: int, current: str) -> dict[tuple[int, str], dict]:
    cells: dict[tuple[int, str], dict] = {}

    def cell(apt: int, period: str) -> dict:
        return cells.setdefault((apt, period), {"due": 0, "paid": 0, "held": [], "bounced": False, "receipt": None, "method": None})

    prefix = f"{year:04d}-%"
    for apt, period, amount in conn.execute(
        "SELECT apt_id, period, amount FROM charges WHERE kind = 'dues' AND period LIKE ?", (prefix,)
    ):
        cell(apt, period)["due"] = amount
    for p in conn.execute("""
        SELECT p.apt_id, p.period, p.amount, p.method, p.status, p.cheque_no, r.no AS receipt
        FROM payments p LEFT JOIN receipts r ON r.payment_id = p.id AND r.voided_at IS NULL
        WHERE p.period LIKE ? ORDER BY p.id
    """, (prefix,)):
        c = cell(p["apt_id"], p["period"])
        if p["status"] == "cleared":
            c["paid"] += p["amount"]
            c["method"] = p["method"]
            c["receipt"] = p["receipt"] or c["receipt"]
        elif p["status"] == "held":
            c["held"].append(p["cheque_no"])
        elif p["status"] == "bounced":
            c["bounced"] = True

    for (apt, period), c in cells.items():
        if c["paid"] and c["paid"] >= c["due"]:
            c["status"] = "paid"
        elif c["held"]:
            c["status"] = "held"
        elif c["paid"]:
            c["status"] = "partial"
        elif c["due"] and period <= current:
            c["status"] = "owed"
        else:
            c["status"] = ""
    return cells


# --- payments and receipts ----------------------------------------------------

def _apartment(conn: sqlite3.Connection, apt_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM apartments WHERE id = ?", (apt_id,)).fetchone()
    if row is None:
        raise Invalid(f"אין דירה {apt_id}")
    return row


def record_payment(conn: sqlite3.Connection, *, apt_id: int, period: str, amount: int, method: str,
                   paid_on: date, now: datetime, cheque_no: str = "", cheque_due: date | None = None,
                   deposited: bool = True, note: str = "") -> int | None:
    """Returns the receipt number, or None for a cheque that is only held so far."""
    _apartment(conn, apt_id)
    check_period(period)
    if method not in ACCOUNT:
        raise Invalid(f"אמצעי תשלום לא מוכר: {method}")
    if amount <= 0:
        raise Invalid("הסכום חייב להיות גדול מאפס")
    cheque_no = cheque_no.strip()
    if method == "cheque" and not cheque_no:
        raise Invalid("חסר מספר צ׳ק")
    status = "held" if method == "cheque" and not deposited else "cleared"
    payment_id = conn.execute(
        "INSERT INTO payments (apt_id, date, amount, method, account, period, cheque_no, cheque_due, status, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (apt_id, paid_on.isoformat(), amount, method, ACCOUNT[method], period,
         cheque_no if method == "cheque" else None,
         cheque_due.isoformat() if method == "cheque" and cheque_due else None,
         status, note.strip()),
    ).lastrowid
    return issue_receipt(conn, payment_id, now) if status == "cleared" else None


def issue_receipt(conn: sqlite3.Connection, payment_id: int, now: datetime) -> int:
    no = conn.execute("SELECT COALESCE(MAX(no), ?) + 1 FROM receipts", (FIRST_RECEIPT - 1,)).fetchone()[0]
    conn.execute(
        "INSERT INTO receipts (no, payment_id, s3_key, issued_at) VALUES (?, ?, ?, ?)",
        (no, payment_id, f"receipts/{now.year}/{no}.pdf", now.isoformat(timespec="seconds")),
    )
    return no


def void_receipt(conn: sqlite3.Connection, no: int, reason: str, now: datetime) -> None:
    if not reason.strip():
        raise Invalid("חובה לכתוב סיבת ביטול")
    row = conn.execute("SELECT payment_id, s3_key, voided_at FROM receipts WHERE no = ?", (no,)).fetchone()
    if row is None:
        raise Invalid(f"אין קבלה {no}")
    if row["voided_at"]:
        raise Invalid(f"קבלה {no} כבר מבוטלת")
    # A new key, so the PDF the resident already has stays untouched in S3.
    conn.execute(
        "UPDATE receipts SET voided_at = ?, void_reason = ?, s3_key = ? WHERE no = ?",
        (now.isoformat(timespec="seconds"), reason.strip(), row["s3_key"].replace(".pdf", "-void.pdf"), no),
    )
    # A bounced cheque keeps 'bounced'; anything else stops counting as paid.
    conn.execute("UPDATE payments SET status = 'void' WHERE id = ? AND status = 'cleared'", (row["payment_id"],))


def receipt(conn: sqlite3.Connection, no: int) -> dict:
    row = conn.execute("""
        SELECT r.*, p.apt_id, p.date, p.amount, p.method, p.period, p.cheque_no, p.cheque_due, p.note,
               a.owner, a.occupant, a.phone
        FROM receipts r JOIN payments p ON p.id = r.payment_id JOIN apartments a ON a.id = p.apt_id
        WHERE r.no = ?
    """, (no,)).fetchone()
    if row is None:
        raise Invalid(f"אין קבלה {no}")
    return {**dict(row), "payer": row["occupant"] or row["owner"]}


def recent_receipts(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT r.no, r.issued_at, r.voided_at, p.apt_id, p.amount, p.method, p.period,
               COALESCE(NULLIF(a.occupant, ''), a.owner) AS payer
        FROM receipts r JOIN payments p ON p.id = r.payment_id JOIN apartments a ON a.id = p.apt_id
        ORDER BY r.no DESC LIMIT ?
    """, (limit,)).fetchall()


# --- cheques ------------------------------------------------------------------

def add_cheque_series(conn: sqlite3.Connection, *, apt_id: int, first_no: int, count: int, amount: int,
                      first_due: date, first_period: str, received_on: date) -> None:
    _apartment(conn, apt_id)
    check_period(first_period)
    if not 1 <= count <= 24:
        raise Invalid("מספר הצ׳קים צריך להיות בין 1 ל-24")
    if amount <= 0:
        raise Invalid("הסכום חייב להיות גדול מאפס")
    conn.executemany(
        "INSERT INTO payments (apt_id, date, amount, method, account, period, cheque_no, cheque_due, status) "
        "VALUES (?, ?, ?, 'cheque', 'bank', ?, ?, ?, 'held')",
        [(apt_id, received_on.isoformat(), amount, period_add(first_period, i), str(first_no + i),
          date_add_months(first_due, i).isoformat()) for i in range(count)],
    )


def held_cheques(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT p.*, COALESCE(NULLIF(a.occupant, ''), a.owner) AS payer
        FROM payments p JOIN apartments a ON a.id = p.apt_id
        WHERE p.status = 'held' ORDER BY p.cheque_due, p.apt_id
    """).fetchall()


def _cheque(conn: sqlite3.Connection, payment_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM payments WHERE id = ? AND method = 'cheque'", (payment_id,)).fetchone()
    if row is None:
        raise Invalid(f"אין צ׳ק {payment_id}")
    return row


def clear_cheque(conn: sqlite3.Connection, payment_id: int, today: date, now: datetime) -> int:
    if _cheque(conn, payment_id)["status"] != "held":
        raise Invalid("אפשר להפקיד רק צ׳ק שממתין")
    conn.execute("UPDATE payments SET status = 'cleared', date = ? WHERE id = ?", (today.isoformat(), payment_id))
    return issue_receipt(conn, payment_id, now)


def bounce_cheque(conn: sqlite3.Connection, payment_id: int, now: datetime) -> None:
    if _cheque(conn, payment_id)["status"] == "bounced":
        raise Invalid("הצ׳ק כבר מסומן כחוזר")
    conn.execute("UPDATE payments SET status = 'bounced' WHERE id = ?", (payment_id,))
    no = conn.execute(
        "SELECT no FROM receipts WHERE payment_id = ? AND voided_at IS NULL", (payment_id,)
    ).fetchone()
    if no:
        void_receipt(conn, no[0], "צ׳ק חזר", now)


def deposit_due(conn: sqlite3.Connection, today: date, now: datetime) -> list[int]:
    due = conn.execute(
        "SELECT id FROM payments WHERE status = 'held' AND cheque_due <= ? ORDER BY cheque_due, apt_id",
        (today.isoformat(),),
    ).fetchall()
    return [clear_cheque(conn, row[0], today, now) for row in due]


# --- apartments ---------------------------------------------------------------

def update_apartment(conn: sqlite3.Connection, apt_id: int, *, owner: str, occupant: str, phone: str,
                     monthly_fee: int, default_method: str, opening: int) -> None:
    _apartment(conn, apt_id)
    if default_method not in ACCOUNT:
        raise Invalid(f"אמצעי תשלום לא מוכר: {default_method}")
    if monthly_fee < 0:
        raise Invalid("דמי ועד לא יכולים להיות שליליים")
    conn.execute(
        "UPDATE apartments SET owner = ?, occupant = ?, phone = ?, monthly_fee = ?, default_method = ? WHERE id = ?",
        (owner.strip(), occupant.strip(), phone.strip(), monthly_fee, default_method, apt_id),
    )
    conn.execute("DELETE FROM charges WHERE apt_id = ? AND kind = 'opening'", (apt_id,))
    if opening:
        conn.execute(
            "INSERT INTO charges (apt_id, period, kind, amount, note) VALUES (?, ?, 'opening', ?, 'יתרת פתיחה')",
            (apt_id, db.settings(conn)["start_period"], opening),
        )


def opening_balances(conn: sqlite3.Connection) -> dict[int, int]:
    return dict(conn.execute("SELECT apt_id, amount FROM charges WHERE kind = 'opening'").fetchall())
