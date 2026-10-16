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
        self.pay(method="cheque", cheque_no="7", deposited=False)
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

    def test_cheque_series_deposit_and_bounce(self):
        ledger.add_cheque_series(self.conn, apt_id=4, first_no=100, count=3, amount=25000,
                                 first_due=date(2026, 12, 31), first_period="2026-12", received_on=date(2026, 11, 1))
        held = ledger.held_cheques(self.conn)
        self.assertEqual([c["cheque_no"] for c in held], ["100", "101", "102"])
        self.assertEqual([c["cheque_due"] for c in held], ["2026-12-31", "2027-01-31", "2027-02-28"])
        self.assertEqual([c["period"] for c in held], ["2026-12", "2027-01", "2027-02"])

        self.assertEqual(ledger.deposit_due(self.conn, date(2027, 1, 31), NOW), [1000, 1001])
        ledger.bounce_cheque(self.conn, held[1]["id"], NOW)
        self.assertEqual(self.conn.execute("SELECT status FROM payments WHERE id = ?", (held[1]["id"],)).fetchone()[0], "bounced")
        self.assertIsNotNone(ledger.receipt(self.conn, 1001)["voided_at"])
        with self.assertRaises(Invalid):
            ledger.clear_cheque(self.conn, held[1]["id"], date(2027, 2, 1), NOW)

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
        self.pay(apt=3, method="cheque", cheque_no="5", deposited=False)
        cells = ledger.grid(self.conn, 2026, "2026-12")
        self.assertEqual(cells[(1, "2026-11")]["status"], "paid")
        self.assertEqual(cells[(2, "2026-11")]["status"], "partial")
        self.assertEqual(cells[(3, "2026-11")]["status"], "held")
        self.assertEqual(cells[(4, "2026-12")]["status"], "owed")

    def test_rejects_bad_input(self):
        for kwargs in ({"amount": 0}, {"method": "gold"}, {"period": "2026-13"}, {"apt": 99},
                       {"method": "cheque"}):
            with self.subTest(kwargs), self.assertRaises(Invalid):
                self.pay(**kwargs)


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
