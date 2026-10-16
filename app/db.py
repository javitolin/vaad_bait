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
"""

SCHEMA = SCHEMA.replace("{payments}", PAYMENTS)

# Applied in order to databases created before the change; a new database gets
# SCHEMA directly. SQLite cannot alter a CHECK, so changing one means a rebuild.
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
]

APARTMENTS = 12
DEFAULT_FEE = 25000

DEFAULT_SETTINGS = {
    "building_name": "ועד הבית",
    "building_address": "",
    "treasurer_name": "",
    "wa_receipt": "שלום {name}, מצורפת קבלה מס׳ {receipt_no} על סך ₪{amount} עבור ועד בית {period}. תודה!",
}


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Foreign keys stay off while migrating: rebuilding payments drops the table receipts point at.
    if conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'payments'").fetchone()[0]:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript(f"BEGIN; {script} PRAGMA user_version = {number}; COMMIT;")
    else:
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS)}")
    conn.executescript(SCHEMA)
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
