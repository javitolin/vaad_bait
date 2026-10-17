import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime

import db
import ledger
from ledger import Invalid

NOW = datetime(2026, 11, 1, 10, 0)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.sqlite"))
        db.save_settings(self.conn, {"start_period": "2026-11"})

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def pay(self, apt=1, period="2026-11", amount=25000, method="cash", **kw):
        return ledger.record_payment(self.conn, apt_id=apt, period=period, amount=amount, method=method,
                                     paid_on=date(2026, 11, 1), now=NOW, **kw)

    def test_balance_is_charges_minus_cleared_payments(self):
        ledger.ensure_dues(self.conn, "2027-01")
        self.assertEqual(ledger.missing_dues(self.conn, "2027-01"), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM charges").fetchone()[0], 36)
        ledger.update_apartment(self.conn, 1, owner="", occupant="", phone="", monthly_fee=25000,
                                default_method="cash", opening=10000)
        self.pay()
        self.pay(method="cheque", deposited=False)
        # 3 months dues + 100 opening - 250 cash; the held cheque does not count yet.
        self.assertEqual(ledger.balances(self.conn)[1], 75000 + 10000 - 25000)

    def test_receipts_start_at_1000_and_voiding_keeps_the_number(self):
        ledger.ensure_dues(self.conn, "2026-11")
        self.assertEqual(self.pay(), 1000)
        self.assertEqual(self.pay(apt=2), 1001)
        ledger.void_receipt(self.conn, 1000, "טעות", NOW)
        self.assertEqual(self.pay(apt=3), 1002)
        self.assertEqual(ledger.balances(self.conn)[1], 25000)
        with self.assertRaises(Invalid):
            ledger.void_receipt(self.conn, 1000, "שוב", NOW)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM receipts WHERE no = 1001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE receipts SET payment_id = 99 WHERE no = 1001")

    def test_mark_then_process_on_the_15th(self):
        ledger.ensure_dues(self.conn, "2026-11")
        cheque = ledger.mark(self.conn, 4, "2026-11", "cheque", date(2026, 11, 2))
        transfer = ledger.mark(self.conn, 5, "2026-11", "transfer", date(2026, 11, 2))
        with self.assertRaises(Invalid):
            ledger.mark(self.conn, 4, "2026-11", "cash", date(2026, 11, 2))
        cells = ledger.grid(self.conn, 2026, "2026-11")
        self.assertEqual((cells[(4, "2026-11")]["status"], cells[(6, "2026-11")]["status"]), ("received", "missing"))
        # Marked is not paid: no receipt yet, and the balance still owes.
        self.assertEqual(ledger.balances(self.conn)[4], 25000)

        with self.assertRaises(Invalid):
            ledger.clear_payment(self.conn, cheque, date(2026, 11, 15), NOW)
        ledger.set_cheque_no(self.conn, cheque, "3340031")
        self.assertEqual(ledger.deposit_due(self.conn, date(2026, 11, 15), NOW), [1000])
        self.assertEqual(ledger.clear_payment(self.conn, transfer, date(2026, 11, 15), NOW), 1001)
        self.assertEqual(ledger.balances(self.conn)[4], 0)
        self.assertEqual(ledger.held_payments(self.conn), [])

        ledger.bounce_cheque(self.conn, cheque, NOW)
        self.assertIsNotNone(ledger.receipt(self.conn, 1000)["voided_at"])
        self.assertEqual(ledger.grid(self.conn, 2026, "2026-11")[(4, "2026-11")]["status"], "missing")

    def test_reminders_skip_what_is_already_in_hand(self):
        ledger.ensure_dues(self.conn, "2026-11")
        ledger.mark(self.conn, 1, "2026-11", "cheque", date(2026, 11, 2))
        self.pay(apt=2, amount=10000)
        owed = {d["apartment"]["id"]: d["owed"] for d in ledger.arrears(self.conn)}
        self.assertNotIn(1, owed)
        self.assertEqual(owed[2], 15000)
        self.assertEqual(len(owed), 11)

    def test_unmark_only_before_a_receipt(self):
        held = ledger.mark(self.conn, 1, "2026-11", "cash", date(2026, 11, 2))
        ledger.unmark(self.conn, held)
        self.assertEqual(ledger.held_payments(self.conn), [])
        paid = ledger.mark(self.conn, 1, "2026-11", "cash", date(2026, 11, 2))
        ledger.clear_payment(self.conn, paid, date(2026, 11, 2), NOW)
        with self.assertRaises(Invalid):
            ledger.unmark(self.conn, paid)

    def test_account_follows_method(self):
        self.pay(method="offset")
        self.pay(method="transfer")
        accounts = [r[0] for r in self.conn.execute("SELECT account FROM payments ORDER BY id")]
        self.assertEqual(accounts, ["treasurer", "bank"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO payments (apt_id, date, amount, method, account, period, status) "
                              "VALUES (1, '2026-11-01', 100, 'cash', 'bank', '2026-11', 'cleared')")

    def test_grid_statuses(self):
        ledger.ensure_dues(self.conn, "2026-12")
        self.pay(apt=1)
        self.pay(apt=2, amount=10000)
        self.pay(apt=3, method="cheque", deposited=False)
        cells = ledger.grid(self.conn, 2026, "2026-12")
        self.assertEqual(cells[(1, "2026-11")]["status"], "paid")
        self.assertEqual(cells[(2, "2026-11")]["status"], "partial")
        self.assertEqual(cells[(3, "2026-11")]["status"], "received")
        self.assertEqual(cells[(4, "2026-12")]["status"], "missing")

    def test_rejects_bad_input(self):
        # A deposited cheque needs its number.
        for kwargs in ({"amount": 0}, {"method": "gold"}, {"period": "2026-13"}, {"apt": 99},
                       {"method": "cheque"}):
            with self.subTest(kwargs), self.assertRaises(Invalid):
                self.pay(**kwargs)


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "t.sqlite"))
        db.save_settings(self.conn, {"start_period": "2026-10", "accounts_start": "2026-11-01",
                                     "opening_bank": "1000000", "opening_treasurer": "-217650"})

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_balances_count_from_the_start_date(self):
        ledger.record_payment(self.conn, apt_id=1, period="2026-10", amount=25000, method="transfer",
                              paid_on=date(2026, 10, 20), now=NOW)  # before the start: already in the opening
        ledger.record_payment(self.conn, apt_id=2, period="2026-11", amount=25000, method="transfer",
                              paid_on=date(2026, 11, 2), now=NOW)
        ledger.record_payment(self.conn, apt_id=6, period="2026-11", amount=25000, method="offset",
                              paid_on=date(2026, 11, 2), now=NOW)
        ledger.add_expense(self.conn, day=date(2026, 11, 3), vendor="", category="גנן", amount=70000, account="treasurer")
        ledger.add_expense(self.conn, day=date(2026, 11, 4), vendor="", category="ביטוח", amount=49100, account="bank")
        ledger.add_transfer(self.conn, day=date(2026, 11, 5), from_account="bank", amount=100000)
        self.assertEqual(ledger.account_balances(self.conn), {
            "bank": 1000000 + 25000 - 49100 - 100000,
            "treasurer": -217650 + 25000 - 70000 + 100000,
        })
        ledger.record_bank_statement(self.conn, date(2026, 11, 6), 875900)
        self.assertEqual(ledger.reconciliation(self.conn)["difference"], 0)

    def test_income_kept_by_the_treasurer_shrinks_the_debt(self):
        ledger.add_income(self.conn, day=date(2026, 11, 10), description="קיזוז", amount=53600,
                          account="treasurer")
        self.assertEqual(ledger.account_balances(self.conn)["treasurer"], -217650 + 53600)
        summary = ledger.year_summary(self.conn, 2026)
        self.assertEqual((summary["other_income"], summary["net"]), (53600, 53600))

    def test_receipts_are_issued_to_the_apartment(self):
        import pdf
        no = ledger.record_payment(self.conn, apt_id=3, period="2026-10", amount=25000, method="cash",
                                   paid_on=date(2026, 10, 1), now=NOW)
        ledger.update_apartment(self.conn, 3, owner="", occupant="דייר חדש", phone="", monthly_fee=25000,
                                default_method="cash", opening=0)
        r = ledger.receipt(self.conn, no)
        self.assertEqual((r["apt_id"], r["name"]), (3, "דייר חדש"))
        self.assertTrue(pdf.receipt_pdf(r, db.settings(self.conn), "s3://b/k").startswith(b"%PDF"))

    def test_recurring_expense_runs_for_its_months_only(self):
        rule = ledger.add_recurring(self.conn, category="ביטוח", vendor="", amount=49100, account="bank",
                                    day=31, start_period="2026-11", months=3)
        ledger.ensure_recurring(self.conn, date(2027, 6, 1))
        ledger.ensure_recurring(self.conn, date(2027, 6, 1))  # idempotent
        dates = [r[0] for r in self.conn.execute("SELECT date FROM expenses WHERE recurring_id = ? ORDER BY date", (rule,))]
        self.assertEqual(dates, ["2026-11-30", "2026-12-31", "2027-01-31"])
        # Nothing before its day: an expense never lands in the balances ahead of time.
        fee = ledger.add_recurring(self.conn, category="עמלות בנק", vendor="", amount=1500, account="bank",
                                   day=20, start_period="2026-11", months=None)
        self.assertEqual(len(ledger.missing_recurring(self.conn, date(2026, 11, 19))), 0)
        ledger.ensure_recurring(self.conn, date(2026, 12, 25))
        ledger.stop_recurring(self.conn, fee)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM expenses WHERE category = 'עמלות בנק'").fetchone()[0], 2)

    def test_only_images_and_pdfs_can_be_attached(self):
        expense = ledger.add_expense(self.conn, day=date(2026, 11, 3), vendor="", category="גנן", amount=100, account="bank")
        self.assertEqual(ledger.attach_file(self.conn, expense, "r.pdf", "application/pdf"), f"expenses/2026/{expense}-1.pdf")
        with self.assertRaises(Invalid):
            ledger.attach_file(self.conn, expense, "x.html", "text/html")

    def test_reports_render(self):
        import pdf
        ledger.ensure_dues(self.conn, "2026-11")
        ledger.record_payment(self.conn, apt_id=1, period="2026-11", amount=25000, method="cheque", cheque_no="9",
                              paid_on=date(2026, 11, 2), now=NOW)
        ledger.add_expense(self.conn, day=date(2026, 11, 3), vendor="", category="גנן", amount=70000, account="treasurer")
        settings = db.settings(self.conn)
        summary = ledger.year_summary(self.conn, 2026)
        self.assertEqual((summary["income"], summary["spent"]), (25000, 70000))
        for body in (pdf.statement_pdf(ledger.statement(self.conn, 1, 2026), settings, "01/11/2026"),
                     pdf.summary_pdf(summary, settings, "01/11/2026", "s3://b/summaries/2026.pdf")):
            self.assertTrue(body.startswith(b"%PDF"))


class MigrationTest(unittest.TestCase):
    def test_old_database_is_rebuilt_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.sqlite")
            old = sqlite3.connect(path)
            old.executescript(db.SCHEMA.replace(db.PAYMENTS, """
                CREATE TABLE payments (id INTEGER PRIMARY KEY, apt_id INTEGER NOT NULL, date TEXT NOT NULL,
                  amount INTEGER NOT NULL, method TEXT NOT NULL, account TEXT NOT NULL, period TEXT NOT NULL,
                  cheque_no TEXT CHECK ((method = 'cheque') = (cheque_no IS NOT NULL)), cheque_due TEXT,
                  status TEXT NOT NULL, note TEXT NOT NULL DEFAULT '');"""))
            old.execute("INSERT INTO apartments (id, monthly_fee) VALUES (1, 25000)")
            old.execute("INSERT INTO payments VALUES (1, 1, '2026-11-01', 25000, 'cheque', 'bank', '2026-11', '—', '2026-11-01', 'held', '')")
            old.execute("INSERT INTO payments VALUES (2, 1, '2026-10-01', 25000, 'cheque', 'bank', '2026-10', '77', NULL, 'cleared', '')")
            old.execute("INSERT INTO receipts VALUES (1000, 2, 'k', '2026-10-01', NULL, NULL)")
            old.commit()
            old.close()

            conn = db.connect(path)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
            self.assertEqual([tuple(r) for r in conn.execute("SELECT id, cheque_no FROM payments ORDER BY id")],
                             [(1, None), (2, "77")])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            ledger.mark(conn, 1, "2026-12", "transfer", date(2026, 12, 1))
            conn.close()


class FormatTest(unittest.TestCase):
    def test_money(self):
        self.assertEqual(ledger.parse_money("1,250.5"), 125050)
        self.assertEqual(ledger.parse_money("-2176.5"), -217650)
        self.assertEqual(ledger.money(-217650), "-2,176.50")
        with self.assertRaises(Invalid):
            ledger.parse_money("12.345")
        with self.assertRaises(Invalid):
            ledger.parse_money("abc")

    def test_fill_and_phone(self):
        self.assertEqual(ledger.fill("{name} {x} {name.__class__}", {"name": "גיל"}), "גיל {x} {name.__class__}")
        self.assertEqual(ledger.wa_phone("050-123 4567"), "972501234567")
        self.assertEqual(ledger.wa_phone(""), "")


if __name__ == "__main__":
    unittest.main()
