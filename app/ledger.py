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


def arrears(conn: sqlite3.Connection) -> list[dict]:
    """Apartments that still owe once what is already in hand (marked, not processed) is counted."""
    held = dict(conn.execute("SELECT apt_id, SUM(amount) FROM payments WHERE status = 'held' GROUP BY apt_id").fetchall())
    apartments = {a["id"]: a for a in conn.execute("SELECT * FROM apartments")}
    return [{"apartment": apartments[apt_id], "owed": balance - held.get(apt_id, 0)}
            for apt_id, balance in sorted(balances(conn).items())
            if balance - held.get(apt_id, 0) > 0]


def grid(conn: sqlite3.Connection, year: int, current: str) -> dict[tuple[int, str], dict]:
    """Per apartment and month: what is due, what came in, and what is still to process."""
    cells: dict[tuple[int, str], dict] = {}

    def cell(apt: int, period: str) -> dict:
        return cells.setdefault((apt, period), {
            "due": 0, "paid": 0, "held_id": None, "method": None, "cheque_no": None,
            "receipt": None, "bounced": False,
        })

    prefix = f"{year:04d}-%"
    for apt, period, amount in conn.execute(
        "SELECT apt_id, period, amount FROM charges WHERE kind = 'dues' AND period LIKE ?", (prefix,)
    ):
        cell(apt, period)["due"] = amount
    for p in conn.execute("""
        SELECT p.id, p.apt_id, p.period, p.amount, p.method, p.status, p.cheque_no, r.no AS receipt
        FROM payments p LEFT JOIN receipts r ON r.payment_id = p.id AND r.voided_at IS NULL
        WHERE p.period LIKE ? ORDER BY p.id
    """, (prefix,)):
        c = cell(p["apt_id"], p["period"])
        if p["status"] == "cleared":
            c["paid"] += p["amount"]
            c["method"] = c["method"] or p["method"]
            c["receipt"] = c["receipt"] or p["receipt"]
        elif p["status"] == "held" and c["held_id"] is None:
            c["held_id"], c["method"], c["cheque_no"] = p["id"], p["method"], p["cheque_no"]
        elif p["status"] == "bounced":
            c["bounced"] = True

    for (apt, period), c in cells.items():
        if c["held_id"]:
            c["status"] = "received"
        elif c["paid"] and c["paid"] >= c["due"]:
            c["status"] = "paid"
        elif c["paid"]:
            c["status"] = "partial"
        elif c["due"] and period <= current:
            c["status"] = "missing"
        else:
            c["status"] = ""
    return cells


# --- payments and receipts ----------------------------------------------------

def payer_name(apartment: sqlite3.Row) -> str:
    return apartment["occupant"] or apartment["owner"]


def _apartment(conn: sqlite3.Connection, apt_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM apartments WHERE id = ?", (apt_id,)).fetchone()
    if row is None:
        raise Invalid(f"אין דירה {apt_id}")
    return row


def record_payment(conn: sqlite3.Connection, *, apt_id: int, period: str, amount: int, method: str,
                   paid_on: date, now: datetime, cheque_no: str = "", deposited: bool = True, note: str = "") -> int | None:
    """Returns the receipt number, or None for a cheque that is only held so far."""
    apartment = _apartment(conn, apt_id)
    check_period(period)
    if method not in ACCOUNT:
        raise Invalid(f"אמצעי תשלום לא מוכר: {method}")
    if amount <= 0:
        raise Invalid("הסכום חייב להיות גדול מאפס")
    cheque_no = cheque_no.strip()
    status = "held" if method == "cheque" and not deposited else "cleared"
    if method == "cheque" and status == "cleared" and not cheque_no:
        raise Invalid("חסר מספר צ׳ק")
    payment_id = conn.execute(
        "INSERT INTO payments (apt_id, date, amount, method, account, period, cheque_no, status, note, payer) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (apt_id, paid_on.isoformat(), amount, method, ACCOUNT[method], period,
         cheque_no if method == "cheque" and cheque_no else None,
         status, note.strip(), payer_name(apartment)),
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
        SELECT r.*, p.apt_id, p.date, p.amount, p.method, p.period, p.cheque_no, p.note,
               COALESCE(NULLIF(p.payer, ''), NULLIF(a.occupant, ''), a.owner) AS payer, a.phone
        FROM receipts r JOIN payments p ON p.id = r.payment_id JOIN apartments a ON a.id = p.apt_id
        WHERE r.no = ?
    """, (no,)).fetchone()
    if row is None:
        raise Invalid(f"אין קבלה {no}")
    return dict(row)


def recent_receipts(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT r.no, r.issued_at, r.voided_at, p.apt_id, p.amount, p.method, p.period,
               COALESCE(NULLIF(p.payer, ''), NULLIF(a.occupant, ''), a.owner) AS payer
        FROM receipts r JOIN payments p ON p.id = r.payment_id JOIN apartments a ON a.id = p.apt_id
        ORDER BY r.no DESC LIMIT ?
    """, (limit,)).fetchall()


# --- marks, cheques and processing ---------------------------------------------

def mark(conn: sqlite3.Connection, apt_id: int, period: str, method: str, today: date) -> int:
    """Received, details later: one tap on the grid. No receipt until it is processed."""
    apartment = _apartment(conn, apt_id)
    check_period(period)
    if method not in ACCOUNT:
        raise Invalid(f"אמצעי תשלום לא מוכר: {method}")
    if apartment["monthly_fee"] <= 0:
        raise Invalid(f"לדירה {apt_id} אין דמי ועד")
    if conn.execute(
        "SELECT 1 FROM payments WHERE apt_id = ? AND period = ? AND status IN ('held', 'cleared')", (apt_id, period)
    ).fetchone():
        raise Invalid(f"דירה {apt_id} כבר מסומנת ל{period_he(period)}")
    return conn.execute(
        "INSERT INTO payments (apt_id, date, amount, method, account, period, status, payer) "
        "VALUES (?, ?, ?, ?, ?, ?, 'held', ?)",
        (apt_id, today.isoformat(), apartment["monthly_fee"], method, ACCOUNT[method], period, payer_name(apartment)),
    ).lastrowid


def _held(conn: sqlite3.Connection, payment_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None or row["status"] != "held":
        raise Invalid("התשלום כבר טופל או לא קיים")
    return row


def unmark(conn: sqlite3.Connection, payment_id: int) -> None:
    # Only a held payment has no receipt yet, so nothing a resident holds disappears.
    _held(conn, payment_id)
    conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))


def set_cheque_no(conn: sqlite3.Connection, payment_id: int, cheque_no: str) -> None:
    row = _held(conn, payment_id)
    if row["method"] != "cheque":
        raise Invalid("רק לצ׳ק יש מספר")
    if not cheque_no.strip():
        raise Invalid("חסר מספר צ׳ק")
    conn.execute("UPDATE payments SET cheque_no = ? WHERE id = ?", (cheque_no.strip(), payment_id))


def clear_payment(conn: sqlite3.Connection, payment_id: int, today: date, now: datetime) -> int:
    """Deposit a cheque or confirm a transfer/cash: the receipt is issued here."""
    row = _held(conn, payment_id)
    if row["method"] == "cheque" and not row["cheque_no"]:
        raise Invalid("קודם להוסיף את מספר הצ׳ק")
    conn.execute("UPDATE payments SET status = 'cleared', date = ? WHERE id = ?", (today.isoformat(), payment_id))
    return issue_receipt(conn, payment_id, now)


def held_payments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        SELECT p.*, COALESCE(NULLIF(a.occupant, ''), a.owner) AS payer
        FROM payments p JOIN apartments a ON a.id = p.apt_id
        WHERE p.status = 'held' ORDER BY p.period, p.apt_id
    """).fetchall()


def bounce_cheque(conn: sqlite3.Connection, payment_id: int, now: datetime) -> None:
    row = conn.execute("SELECT status FROM payments WHERE id = ? AND method = 'cheque'", (payment_id,)).fetchone()
    if row is None:
        raise Invalid(f"אין צ׳ק {payment_id}")
    if row["status"] == "bounced":
        raise Invalid("הצ׳ק כבר מסומן כחוזר")
    conn.execute("UPDATE payments SET status = 'bounced' WHERE id = ?", (payment_id,))
    no = conn.execute(
        "SELECT no FROM receipts WHERE payment_id = ? AND voided_at IS NULL", (payment_id,)
    ).fetchone()
    if no:
        void_receipt(conn, no[0], "צ׳ק חזר", now)


def deposit_due(conn: sqlite3.Connection, today: date, now: datetime) -> list[int]:
    """Deposits every numbered cheque held for this month or earlier."""
    due = conn.execute(
        "SELECT id FROM payments WHERE status = 'held' AND method = 'cheque' AND cheque_no IS NOT NULL "
        "AND period <= ? ORDER BY period, apt_id",
        (today.strftime("%Y-%m"),),
    ).fetchall()
    return [clear_payment(conn, row[0], today, now) for row in due]


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


# --- expenses, transfers and the two accounts -------------------------------------

ACCOUNTS = {"bank": "חשבון הבנק", "treasurer": "קופת גזבר"}


def add_expense(conn: sqlite3.Connection, *, day: date, vendor: str, category: str, amount: int,
                account: str, note: str = "") -> int:
    if account not in ACCOUNTS:
        raise Invalid(f"חשבון לא מוכר: {account}")
    if amount <= 0:
        raise Invalid("הסכום חייב להיות גדול מאפס")
    if not category.strip():
        raise Invalid("חסרה קטגוריה")
    return conn.execute(
        "INSERT INTO expenses (date, vendor, category, amount, account, note) VALUES (?, ?, ?, ?, ?, ?)",
        (day.isoformat(), vendor.strip(), category.strip(), amount, account, note.strip()),
    ).lastrowid


def attach_file(conn: sqlite3.Connection, expense_id: int, name: str, content_type: str) -> str:
    """Reserves the key for a vendor receipt; the caller stores the bytes under it."""
    row = conn.execute("SELECT date FROM expenses WHERE id = ?", (expense_id,)).fetchone()
    if row is None:
        raise Invalid(f"אין הוצאה {expense_id}")
    n = conn.execute("SELECT COUNT(*) FROM expense_files WHERE expense_id = ?", (expense_id,)).fetchone()[0] + 1
    extension = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                 "image/heic": "heic"}.get(content_type)
    if extension is None:
        raise Invalid("אפשר לצרף תמונה או PDF בלבד")
    key = f"expenses/{row['date'][:4]}/{expense_id}-{n}.{extension}"
    conn.execute("INSERT INTO expense_files (expense_id, s3_key, content_type, name) VALUES (?, ?, ?, ?)",
                 (expense_id, key, content_type, name))
    return key


def expense_file(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM expense_files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise Invalid("הקובץ לא נמצא")
    return row


def expenses(conn: sqlite3.Connection, year: int) -> list[dict]:
    files: dict[int, list] = {}
    for f in conn.execute("SELECT * FROM expense_files ORDER BY id"):
        files.setdefault(f["expense_id"], []).append(f)
    return [{**dict(e), "files": files.get(e["id"], [])} for e in conn.execute(
        "SELECT * FROM expenses WHERE date LIKE ? ORDER BY date DESC, id DESC", (f"{year:04d}-%",))]


def delete_expense(conn: sqlite3.Connection, expense_id: int) -> None:
    # The attached files stay in S3: nothing in the app deletes stored documents.
    conn.execute("DELETE FROM expense_files WHERE expense_id = ?", (expense_id,))
    conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))


def add_recurring(conn: sqlite3.Connection, *, category: str, vendor: str, amount: int, account: str,
                  day: int, start_period: str, months: int | None, note: str = "") -> int:
    if account not in ACCOUNTS:
        raise Invalid(f"חשבון לא מוכר: {account}")
    if amount <= 0:
        raise Invalid("הסכום חייב להיות גדול מאפס")
    if not 1 <= day <= 31:
        raise Invalid("יום בחודש צריך להיות בין 1 ל-31")
    if months is not None and months <= 0:
        raise Invalid("מספר החודשים צריך להיות חיובי, או ריק ללא הגבלה")
    if not category.strip():
        raise Invalid("חסרה קטגוריה")
    return conn.execute(
        "INSERT INTO recurring (category, vendor, amount, account, day, start_period, months, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (category.strip(), vendor.strip(), amount, account, day, check_period(start_period), months, note.strip()),
    ).lastrowid


def recurring_rules(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT r.*, (SELECT COUNT(*) FROM expenses e WHERE e.recurring_id = r.id) AS made
        FROM recurring r ORDER BY r.start_period, r.id
    """)]


def stop_recurring(conn: sqlite3.Connection, rule_id: int) -> None:
    conn.execute("DELETE FROM recurring WHERE id = ?", (rule_id,))


def missing_recurring(conn: sqlite3.Connection, today: date) -> list[tuple[sqlite3.Row, date]]:
    """Each rule's months whose day has come and that have no expense yet."""
    have = {tuple(row) for row in conn.execute(
        "SELECT recurring_id, substr(date, 1, 7) FROM expenses WHERE recurring_id IS NOT NULL")}
    missing = []
    for rule in conn.execute("SELECT * FROM recurring"):
        i = 0
        while rule["months"] is None or i < rule["months"]:
            period = period_add(rule["start_period"], i)
            year, month = int(period[:4]), int(period[5:])
            day = date(year, month, min(rule["day"], calendar.monthrange(year, month)[1]))
            if day > today:
                break
            if (rule["id"], period) not in have:
                missing.append((rule, day))
            i += 1
    return missing


def ensure_recurring(conn: sqlite3.Connection, today: date) -> None:
    for rule, day in missing_recurring(conn, today):
        conn.execute(
            "INSERT OR IGNORE INTO expenses (date, vendor, category, amount, account, note, recurring_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (day.isoformat(), rule["vendor"], rule["category"], rule["amount"], rule["account"], rule["note"], rule["id"]),
        )


def add_transfer(conn: sqlite3.Connection, *, day: date, from_account: str, amount: int,
                 cheque_no: str = "", note: str = "") -> None:
    if from_account not in ACCOUNTS:
        raise Invalid(f"חשבון לא מוכר: {from_account}")
    if amount <= 0:
        raise Invalid("הסכום חייב להיות גדול מאפס")
    to_account = "treasurer" if from_account == "bank" else "bank"
    conn.execute(
        "INSERT INTO transfers (date, from_account, to_account, amount, cheque_no, note) VALUES (?, ?, ?, ?, ?, ?)",
        (day.isoformat(), from_account, to_account, amount, cheque_no.strip() or None, note.strip()),
    )


def account_balances(conn: sqlite3.Connection, as_of: str = "9999-12-31") -> dict[str, int] | None:
    """Opening + money in - money out since accounts_start; None until the openings are set."""
    s = db.settings(conn)
    start = s.get("accounts_start")
    if not start:
        return None
    window = (start, as_of)
    result = {}
    for account in ACCOUNTS:
        income = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE account = ? AND status = 'cleared' "
            "AND date BETWEEN ? AND ?", (account, *window)).fetchone()[0]
        spent = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE account = ? AND date BETWEEN ? AND ?",
            (account, *window)).fetchone()[0]
        moved_in = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transfers WHERE to_account = ? AND date BETWEEN ? AND ?",
            (account, *window)).fetchone()[0]
        moved_out = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transfers WHERE from_account = ? AND date BETWEEN ? AND ?",
            (account, *window)).fetchone()[0]
        result[account] = int(s[f"opening_{account}"]) + income - spent + moved_in - moved_out
    return result


def record_bank_statement(conn: sqlite3.Connection, day: date, balance: int) -> None:
    conn.execute("INSERT INTO bank_statements (date, balance) VALUES (?, ?) "
                 "ON CONFLICT(date) DO UPDATE SET balance = excluded.balance", (day.isoformat(), balance))


def reconciliation(conn: sqlite3.Connection) -> dict | None:
    """The latest paper statement against what the books say the bank held that day."""
    row = conn.execute("SELECT * FROM bank_statements ORDER BY date DESC LIMIT 1").fetchone()
    books = account_balances(conn, row["date"]) if row else None
    if not row or books is None:
        return None
    return {"date": row["date"], "statement": row["balance"], "books": books["bank"],
            "difference": row["balance"] - books["bank"]}


# --- per-apartment statement and the yearly summary --------------------------------

def statement(conn: sqlite3.Connection, apt_id: int, year: int) -> dict:
    apartment = _apartment(conn, apt_id)
    months = []
    for m in range(1, 13):
        period = f"{year:04d}-{m:02d}"
        due = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM charges WHERE apt_id = ? AND period = ? "
                           "AND kind = 'dues'", (apt_id, period)).fetchone()[0]
        payments = conn.execute("""
            SELECT p.*, r.no AS receipt FROM payments p
            LEFT JOIN receipts r ON r.payment_id = p.id AND r.voided_at IS NULL
            WHERE p.apt_id = ? AND p.period = ? AND p.status IN ('cleared', 'held') ORDER BY p.id
        """, (apt_id, period)).fetchall()
        months.append({"period": period, "due": due, "payments": payments,
                       "paid": sum(p["amount"] for p in payments if p["status"] == "cleared")})
    return {
        "apartment": apartment,
        "name": payer_name(apartment),
        "year": year,
        "months": months,
        "due": sum(m["due"] for m in months),
        "paid": sum(m["paid"] for m in months),
        "balance": balances(conn)[apt_id],
    }


def year_summary(conn: sqlite3.Connection, year: int) -> dict:
    prefix = f"{year:04d}-%"
    rows = []
    for a in conn.execute("SELECT * FROM apartments ORDER BY id"):
        paid = conn.execute("SELECT COALESCE(SUM(amount), 0), COUNT(DISTINCT period) FROM payments "
                            "WHERE apt_id = ? AND status = 'cleared' AND period LIKE ?", (a["id"], prefix)).fetchone()
        due = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM charges WHERE apt_id = ? AND kind = 'dues' "
                           "AND period LIKE ?", (a["id"], prefix)).fetchone()[0]
        rows.append({"apt": a["id"], "name": payer_name(a), "months": paid[1], "paid": paid[0], "due": due})
    by_category = conn.execute(
        "SELECT category, SUM(amount) AS amount FROM expenses WHERE date LIKE ? GROUP BY category ORDER BY amount DESC",
        (prefix,)).fetchall()
    income = sum(r["paid"] for r in rows)
    spent = sum(c["amount"] for c in by_category)
    return {"year": year, "apartments": rows, "income": income, "expenses": by_category, "spent": spent,
            "net": income - spent, "accounts": account_balances(conn, f"{year:04d}-12-31")}
