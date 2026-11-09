"""
Підписка: розрахунок комісії з перенесенням, рахунки за періоди, блокування
входів. Реальна БД не потрібна.

Запуск: python -m unittest tests.test_billing -v
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")

from pymongo.errors import DuplicateKeyError  # noqa: E402

from core.billing.calculator import calculate_period  # noqa: E402
from core.billing.service import BillingService, public_invoice  # noqa: E402
from core.users.access import can_open_new  # noqa: E402

START = datetime(2026, 1, 1)


class TestCalculator(unittest.TestCase):
    def test_profit_week(self):
        r = calculate_period(12.0, 0.0, 10.0, 1.0)
        self.assertEqual((r.chargeable, r.amount, r.carry_after), (12.0, 1.2, 0.0))

    def test_loss_week_no_charge_and_carries(self):
        r = calculate_period(-8.0, 0.0, 10.0, 1.0)
        self.assertEqual((r.amount, r.carry_after), (0.0, -8.0))

    def test_loss_must_be_recovered_first(self):
        r = calculate_period(5.0, -8.0, 10.0, 1.0)
        self.assertEqual((r.amount, r.carry_after), (0.0, -3.0))
        r = calculate_period(23.0, -3.0, 10.0, 1.0)
        self.assertEqual((r.chargeable, r.amount, r.carry_after), (20.0, 2.0, 0.0))

    def test_small_fee_carried_not_lost(self):
        r = calculate_period(5.0, 0.0, 10.0, 1.0)  # 0.50 < мінімуму 1.00
        self.assertEqual((r.amount, r.carry_after), (0.0, 5.0))
        r = calculate_period(6.0, r.carry_after, 10.0, 1.0)
        self.assertEqual((r.chargeable, r.amount, r.carry_after), (11.0, 1.1, 0.0))


class _Invoices:
    """Достатньо Mongo для сервісу, включно з унікальністю (user_id, period_end)."""
    def __init__(self):
        self.docs = []

    def create_index(self, *a, **k):
        pass

    def insert_one(self, doc):
        if any(d["user_id"] == doc["user_id"] and d["period_end"] == doc["period_end"] for d in self.docs):
            raise DuplicateKeyError("dup")
        doc["_id"] = f"inv{len(self.docs)}"
        self.docs.append(doc)

    def find_one(self, flt):
        return next((d for d in self.docs if all(d.get(k) == v for k, v in flt.items())), None)

    def count_documents(self, flt, limit=0):
        return sum(1 for d in self.docs if all(d.get(k) == v for k, v in flt.items()))


def _service(rows):
    client = mock.MagicMock()
    base = mock.MagicMock()
    base.invoices = _Invoices()
    base.control.find_one.return_value = {"fee_percent": 10.0}
    positions = mock.MagicMock()
    positions.positions.find.return_value = rows
    client.__getitem__.side_effect = lambda name: positions if name == "live" else base
    users = mock.MagicMock()
    with mock.patch("core.billing.service.base_db_name", return_value="base"), \
            mock.patch("core.billing.service.db_name_for", return_value="live"):
        service = BillingService(users, client)
    return service, base.invoices, users, positions


def _row(day, net):
    return {"closed_at": START + timedelta(days=day), "net_pnl": net, "status": "CLOSED"}


class TestBillUser(unittest.TestCase):
    def _bill(self, service, user, now):
        with mock.patch("core.billing.service.db_name_for", return_value="live"):
            return service.bill_user(user, now)

    def _user(self, **extra):
        return {"_id": "1", "role": "user", "trial_until": START, **extra}

    def test_nothing_during_first_period(self):
        service, invoices, _, _ = _service([_row(1, 50.0)])
        self.assertEqual(self._bill(service, self._user(), START + timedelta(days=3)), [])
        self.assertEqual(invoices.docs, [])

    def test_trial_profit_not_charged(self):
        # угода закрилась до кінця пробного періоду
        service, invoices, _, _ = _service([_row(-2, 100.0)])
        self.assertEqual(self._bill(service, self._user(), START + timedelta(days=8)), [])
        self.assertEqual(invoices.docs[0]["status"], "no_charge")

    def test_profit_week_issues_invoice_and_marks_unpaid(self):
        service, invoices, users, _ = _service([_row(1, 30.0), _row(2, -10.0)])
        issued = self._bill(service, self._user(), START + timedelta(days=8))
        self.assertEqual(len(issued), 1)
        self.assertEqual(issued[0]["amount_usdt"], 2.0)
        self.assertEqual(invoices.docs[0]["status"], "open")
        self.assertEqual(users.collection.update_one.call_args.args[1], {"$set": {"billing.unpaid": True}})

    def test_trade_on_boundary_counted_once(self):
        service, invoices, _, _ = _service([_row(7, 20.0)])
        self._bill(service, self._user(), START + timedelta(days=15))
        self.assertEqual([d["net_pnl"] for d in invoices.docs], [0.0, 20.0])

    def test_two_weeks_carry_through(self):
        service, invoices, _, _ = _service([_row(1, -8.0), _row(8, 28.0)])
        issued = self._bill(service, self._user(), START + timedelta(days=15))
        self.assertEqual([i["chargeable"] for i in issued], [20.0])

    def test_rerun_after_crash_does_not_duplicate(self):
        service, invoices, _, _ = _service([_row(1, 30.0)])
        user = self._user()
        self._bill(service, user, START + timedelta(days=8))
        # користувача не оновили: той самий період рахується вдруге
        self.assertEqual(self._bill(service, user, START + timedelta(days=8)), [])
        self.assertEqual(len(invoices.docs), 1)

    def test_admin_exempt(self):
        service, invoices, _, _ = _service([_row(1, 30.0)])
        self.assertEqual(self._bill(service, self._user(role="admin"), START + timedelta(days=8)), [])
        self.assertEqual(invoices.docs, [])

    def test_string_closed_at_supported(self):
        service, invoices, _, _ = _service([
            {"closed_at": (START + timedelta(days=1)).isoformat(), "net_pnl": 30.0, "status": "CLOSED"},
        ])
        self._bill(service, self._user(), START + timedelta(days=8))
        self.assertEqual(invoices.docs[0]["net_pnl"], 30.0)


class TestAccessWithBilling(unittest.TestCase):
    def test_unpaid_blocks_new_entries(self):
        allowed, reason = can_open_new({"status": "active", "billing": {"unpaid": True}})
        self.assertFalse(allowed)
        self.assertIn("рахунок", reason)

    def test_paid_allows(self):
        self.assertTrue(can_open_new({"status": "active", "billing": {"unpaid": False}})[0])

    def test_admin_never_blocked_by_billing(self):
        self.assertTrue(can_open_new({"status": "active", "role": "admin", "billing": {"unpaid": True}})[0])


class TestPublicInvoice(unittest.TestCase):
    def test_id_is_string(self):
        view = public_invoice({"_id": 123, "amount_usdt": 1.0})
        self.assertEqual(view["id"], "123")
        self.assertIsNone(view["_id"])


if __name__ == "__main__":
    unittest.main()
