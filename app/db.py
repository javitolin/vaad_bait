import sqlite3
from datetime import date

PAYMENTS = """
CREATE TABLE IF NOT EXISTS payments (
  id         INTEGER PRIMARY KEY,
  apt_id     INTEGER NOT NULL REFERENCES apartments(id),
  date       TEXT NOT NULL,
  amount     INTEGER NOT NULL CHECK (amount > 0),
  method     TEXT NOT NULL CHECK (method IN ('transfer','cheque','cash','bit','offset')),
  account    TEXT NOT NULL CHECK (account = CASE WHEN method IN ('transfer','cheque') THEN 'bank' ELSE 'treasurer' END),
  period     TEXT NOT NULL CHECK (period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  -- A cheque's number can wait until it is deposited; nothing else has one.
  cheque_no  TEXT CHECK (method = 'cheque' OR cheque_no IS NULL),
  cheque_due TEXT,
  -- 'held': received, no receipt yet. 'void': its receipt was voided, so it no longer counts.
  status     TEXT NOT NULL CHECK (status IN ('held','cleared','bounced','void')),
  note       TEXT NOT NULL DEFAULT '',
  CHECK (status <> 'bounced' OR method = 'cheque'),
  CHECK (NOT (method = 'cheque' AND status = 'cleared' AND cheque_no IS NULL))
);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartments (
  id             INTEGER PRIMARY KEY,
  owner          TEXT NOT NULL DEFAULT '',
  occupant       TEXT NOT NULL DEFAULT '',
  phone          TEXT NOT NULL DEFAULT '',
  monthly_fee    INTEGER NOT NULL CHECK (monthly_fee >= 0),
  default_method TEXT NOT NULL DEFAULT 'cheque'
                 CHECK (default_method IN ('transfer','cheque','cash','bit','offset'))
);

CREATE TABLE IF NOT EXISTS charges (
  id     INTEGER PRIMARY KEY,
  apt_id INTEGER NOT NULL REFERENCES apartments(id),
  period TEXT NOT NULL CHECK (period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  kind   TEXT NOT NULL CHECK (kind IN ('dues','other','opening')),
  amount INTEGER NOT NULL,
  note   TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS charges_one_dues_per_month ON charges(apt_id, period) WHERE kind = 'dues';
CREATE UNIQUE INDEX IF NOT EXISTS charges_one_opening ON charges(apt_id) WHERE kind = 'opening';

{payments}
CREATE TABLE IF NOT EXISTS receipts (
  no          INTEGER PRIMARY KEY,
  payment_id  INTEGER NOT NULL UNIQUE REFERENCES payments(id),
  s3_key      TEXT NOT NULL,
  issued_at   TEXT NOT NULL,
  voided_at   TEXT,
  void_reason TEXT
);

-- A receipt number, once handed to a resident, is permanent. The only change
-- allowed is voiding it, once.
CREATE TRIGGER IF NOT EXISTS receipts_never_deleted BEFORE DELETE ON receipts
BEGIN SELECT RAISE(ABORT, 'receipts are never deleted'); END;

CREATE TRIGGER IF NOT EXISTS receipts_only_void BEFORE UPDATE ON receipts
WHEN OLD.voided_at IS NOT NULL OR NEW.voided_at IS NULL
  OR NEW.no IS NOT OLD.no OR NEW.payment_id IS NOT OLD.payment_id OR NEW.issued_at IS NOT OLD.issued_at
BEGIN SELECT RAISE(ABORT, 'a receipt can only be voided, once'); END;

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
  id       INTEGER PRIMARY KEY,
  date     TEXT NOT NULL,
  vendor   TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL,
  amount   INTEGER NOT NULL CHECK (amount > 0),
  account  TEXT NOT NULL CHECK (account IN ('bank','treasurer')),
  note     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS expense_files (
  id           INTEGER PRIMARY KEY,
  expense_id   INTEGER NOT NULL REFERENCES expenses(id),
  s3_key       TEXT NOT NULL UNIQUE,
  content_type TEXT NOT NULL,
  name         TEXT NOT NULL DEFAULT ''
);

-- Money moving between the ועד's two accounts, e.g. a cheque reimbursing the treasurer.
CREATE TABLE IF NOT EXISTS transfers (
  id           INTEGER PRIMARY KEY,
  date         TEXT NOT NULL,
  from_account TEXT NOT NULL CHECK (from_account IN ('bank','treasurer')),
  to_account   TEXT NOT NULL CHECK (to_account IN ('bank','treasurer') AND to_account <> from_account),
  amount       INTEGER NOT NULL CHECK (amount > 0),
  cheque_no    TEXT,
  note         TEXT NOT NULL DEFAULT ''
);

-- A monthly expense that repeats (bank fee, insurance instalments). Each month's
-- expense row is created when its day arrives; months NULL means until stopped.
CREATE TABLE IF NOT EXISTS recurring (
  id           INTEGER PRIMARY KEY,
  category     TEXT NOT NULL,
  vendor       TEXT NOT NULL DEFAULT '',
  amount       INTEGER NOT NULL CHECK (amount > 0),
  account      TEXT NOT NULL CHECK (account IN ('bank','treasurer')),
  day          INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31),
  start_period TEXT NOT NULL CHECK (start_period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
  months       INTEGER CHECK (months IS NULL OR months > 0),
  note         TEXT NOT NULL DEFAULT ''
);

-- Typed in from the paper statement, to compare against what the books say.
CREATE TABLE IF NOT EXISTS bank_statements (
  date    TEXT PRIMARY KEY,
  balance INTEGER NOT NULL
);
"""

SCHEMA = SCHEMA.replace("{payments}", PAYMENTS)

# SCHEMA is the version-1 layout and must stay that way: a new database is created
# from it and then runs MIGRATIONS[1:]. Changes to existing tables go here, in
# order; brand-new tables can just be added to SCHEMA. SQLite cannot alter a CHECK,
# so changing one means a rebuild.
BASELINE = 1
MIGRATIONS = [
    # 1: any method can be marked received before its receipt; cheque numbers can wait.
    PAYMENTS.replace("IF NOT EXISTS payments", "payments_v1") + """
    INSERT INTO payments_v1 (id, apt_id, date, amount, method, account, period, cheque_no, cheque_due, status, note)
      SELECT id, apt_id, date, amount, method, account, period,
             CASE WHEN status = 'held' AND cheque_no = '—' THEN NULL ELSE cheque_no END,
             cheque_due, status, note
      FROM payments;
    DROP TABLE payments;
    ALTER TABLE payments_v1 RENAME TO payments;
    """,
    # 2: who paid, as they were named then, so a new tenant never appears on an old receipt.
    """
    ALTER TABLE payments ADD COLUMN payer TEXT NOT NULL DEFAULT '';
    UPDATE payments SET payer = (
      SELECT COALESCE(NULLIF(occupant, ''), owner) FROM apartments WHERE apartments.id = payments.apt_id
    );
    """,
    # 3: expenses created by a recurring rule, at most one per rule and month. Stopping
    # a rule keeps the expenses it already made.
    """
    ALTER TABLE expenses ADD COLUMN recurring_id INTEGER REFERENCES recurring(id) ON DELETE SET NULL;
    CREATE UNIQUE INDEX expenses_one_per_rule_month ON expenses(recurring_id, substr(date, 1, 7))
      WHERE recurring_id IS NOT NULL;
    """,
    # 4: cheques are tracked by the month they pay for, not a due date.
    """
    ALTER TABLE payments DROP COLUMN cheque_due;
    """,
]

APARTMENTS = 12
DEFAULT_FEE = 25000

DEFAULT_SETTINGS = {
    "building_name": "ועד הבית",
    "building_address": "",
    "treasurer_name": "",
    "wa_receipt": "שלום {name}, מצורפת קבלה מס׳ {receipt_no} על סך ₪{amount} עבור ועד בית {period}. תודה!",
    "wa_statement": "שלום {name}, מצורף אישור על התשלומים לוועד הבית לשנת {year}. תודה!",
    "wa_reminder": "שלום {name}, תזכורת ידידותית: יתרת דמי ועד הבית לדירה {apt} היא ₪{balance}. תודה!",
    "wa_summary": "שלום לכולם, מצורף הסיכום השנתי של ועד הבית לשנת {year}.",
    "expense_categories": "גנן\nמנקה\nביטוח\nמעלית שירות\nמעלית שנתי\nאגודה לתרבות הדיור\nשונות",
    # Account balances count from this date; the openings are what each held on it.
    "accounts_start": "",
    "opening_bank": "0",
    "opening_treasurer": "0",
}


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Foreign keys stay off while migrating: rebuilding payments drops the table receipts point at.
    fresh = not conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'payments'").fetchone()[0]
    # First, so tables added to SCHEMA later exist before a migration alters them.
    conn.executescript(SCHEMA)
    version = BASELINE if fresh else conn.execute("PRAGMA user_version").fetchone()[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(f"BEGIN; {script} PRAGMA user_version = {number}; COMMIT;")
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("SELECT COUNT(*) FROM apartments").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO apartments (id, monthly_fee) VALUES (?, ?)",
            [(n, DEFAULT_FEE) for n in range(1, APARTMENTS + 1)],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            [*DEFAULT_SETTINGS.items(), ("start_period", date.today().strftime("%Y-%m"))],
        )
        conn.commit()
    return conn


def settings(conn: sqlite3.Connection) -> dict[str, str]:
    return {**DEFAULT_SETTINGS, **dict(conn.execute("SELECT key, value FROM settings").fetchall())}


def save_settings(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        values.items(),
    )
