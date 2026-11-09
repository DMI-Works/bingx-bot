"""
Підписка: тиждень безкоштовно, далі щотижневий рахунок на % від чистого
прибутку на live-рахунку. Поки є неоплачений рахунок, нові входи заборонені
(core/users/access.py), відкриті позиції супроводжуються далі.

Період — 7 днів від кінця пробного періоду кожного користувача. Угода належить
періоду, в якому вона ЗАКРИЛАСЬ. Testnet не тарифікується, адмін теж.

Сплата поки лише вручну (адмін позначає рахунок оплаченим). Автосписання з
BingX підключиться окремо: воно має викликати mark_paid(method='bingx').
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.errors import DuplicateKeyError

from ..database.mongo_config import base_db_name, db_name_for
from .calculator import calculate_period

logger = logging.getLogger(__name__)

PERIOD = timedelta(days=7)
CHECK_INTERVAL_SECONDS = 3600
DEFAULT_FEE_PERCENT = 10.0
DEFAULT_MIN_INVOICE = 1.0
CONFIG_ID = "billing"

OPEN, PAID, WAIVED, NO_CHARGE = "open", "paid", "waived", "no_charge"


def _parse_dt(value) -> Optional[datetime]:
    """closed_at у старих записах може бути рядком."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _net(row: dict) -> float:
    value = row.get("net_pnl")
    if value is None:
        value = row.get("realized_pnl")
    return float(value or 0.0)


def public_invoice(doc: dict) -> dict:
    return {**doc, "_id": None, "id": str(doc["_id"])}


class BillingService:
    def __init__(self, users, mongo_client, sessions=None, notify=None):
        """notify: async (user_id, text) -> None."""
        self.users = users
        self.client = mongo_client
        self.sessions = sessions
        self.notify = notify
        base = mongo_client[base_db_name()]
        self.invoices = base.invoices
        self.control = base.control
        self.invoices.create_index([("user_id", 1), ("period_end", 1)], unique=True)
        self.invoices.create_index([("status", 1), ("created_at", DESCENDING)])
        self._task: Optional[asyncio.Task] = None

    # ---------- налаштування ----------

    def config(self) -> dict:
        doc = self.control.find_one({"_id": CONFIG_ID}) or {}
        return {
            "fee_percent": float(doc.get("fee_percent", DEFAULT_FEE_PERCENT)),
            "min_invoice_usdt": float(doc.get("min_invoice_usdt", DEFAULT_MIN_INVOICE)),
        }

    def set_fee_percent(self, value: float) -> dict:
        self.control.update_one({"_id": CONFIG_ID}, {"$set": {"fee_percent": float(value)}}, upsert=True)
        return self.config()

    # ---------- дані ----------

    def _closed_rows(self, user_id: str) -> List[dict]:
        return list(self.client[db_name_for(False)].positions.find({"user_id": str(user_id), "status": "CLOSED"}))

    def _net_between(self, rows: List[dict], start: datetime, end: datetime) -> float:
        total = 0.0
        for row in rows:
            closed_at = _parse_dt(row.get("closed_at"))
            if closed_at is not None and start <= closed_at < end:
                total += _net(row)
        return round(total, 8)

    @staticmethod
    def _period_start(user: dict) -> Optional[datetime]:
        billing = user.get("billing") or {}
        return billing.get("billed_until") or user.get("trial_until")

    def _refresh_unpaid(self, user_id: str) -> bool:
        unpaid = self.invoices.count_documents({"user_id": str(user_id), "status": OPEN}, limit=1) > 0
        self.users.collection.update_one({"_id": str(user_id)}, {"$set": {"billing.unpaid": unpaid}})
        return unpaid

    # ---------- виставлення рахунків ----------

    def bill_user(self, user: dict, now: Optional[datetime] = None) -> List[dict]:
        """Закриває всі завершені періоди користувача. Повертає нові рахунки до оплати."""
        if user.get("role") == "admin":
            return []
        start = self._period_start(user)
        if start is None or start + PERIOD > (now or datetime.utcnow()):
            return []

        now = now or datetime.utcnow()
        user_id = str(user["_id"])
        cfg = self.config()
        rows = self._closed_rows(user_id)
        carry = float((user.get("billing") or {}).get("carry_pnl", 0.0))
        issued = []

        while start + PERIOD <= now:
            end = start + PERIOD
            net = self._net_between(rows, start, end)
            result = calculate_period(net, carry, cfg["fee_percent"], cfg["min_invoice_usdt"])
            doc = {
                "user_id": user_id,
                "period_start": start,
                "period_end": end,
                "net_pnl": net,
                "carry_before": carry,
                "carry_after": result.carry_after,
                "chargeable": result.chargeable,
                "fee_percent": cfg["fee_percent"],
                "amount_usdt": result.amount,
                "status": OPEN if result.amount > 0 else NO_CHARGE,
                "created_at": now,
            }
            try:
                self.invoices.insert_one(doc)
                if doc["status"] == OPEN:
                    issued.append(doc)
            except DuplicateKeyError:
                # минулий запуск обірвався між вставкою і оновленням користувача
                doc = self.invoices.find_one({"user_id": user_id, "period_end": end})
            carry = float(doc["carry_after"])
            start = end
            self.users.collection.update_one(
                {"_id": user_id},
                {"$set": {"billing.billed_until": start, "billing.carry_pnl": carry}},
            )

        self._refresh_unpaid(user_id)
        return issued

    def run_once(self, now: Optional[datetime] = None) -> List[dict]:
        issued = []
        for user in self.users.list_all():
            try:
                issued += self.bill_user(user, now)
            except Exception as e:
                logger.error(f"[BILLING] failed for user {user.get('_id')}: {e}", exc_info=True)
        return issued

    # ---------- статус і оплата ----------

    def user_status(self, user: dict) -> dict:
        user_id = str(user["_id"])
        cfg = self.config()
        is_admin = user.get("role") == "admin"
        start = self._period_start(user)
        now = datetime.utcnow()
        trial_until = user.get("trial_until")
        carry = float((user.get("billing") or {}).get("carry_pnl", 0.0))

        current = None
        if not is_admin and start is not None and start <= now:
            net = self._net_between(self._closed_rows(user_id), start, now)
            estimate = calculate_period(net, carry, cfg["fee_percent"], cfg["min_invoice_usdt"])
            current = {
                "start": start, "end": start + PERIOD, "net_pnl": net, "estimated_fee": estimate.amount,
            }

        history = list(
            self.invoices.find({"user_id": user_id, "status": {"$ne": NO_CHARGE}})
            .sort("period_end", DESCENDING).limit(20)
        )
        return {
            "exempt": is_admin,
            "in_trial": bool(not is_admin and trial_until and trial_until > now),
            "trial_until": trial_until,
            "fee_percent": cfg["fee_percent"],
            "carry_pnl": carry,
            "unpaid": bool((user.get("billing") or {}).get("unpaid")),
            "current_period": current,
            "invoices": [public_invoice(i) for i in history],
        }

    def open_invoices(self) -> List[dict]:
        return [public_invoice(i) for i in self.invoices.find({"status": OPEN}).sort("created_at", DESCENDING)]

    def _close_invoice(self, invoice_id: str, status: str, method: str, by: str) -> Optional[dict]:
        doc = self.invoices.find_one_and_update(
            {"_id": ObjectId(invoice_id), "status": OPEN},
            {"$set": {"status": status, "paid_at": datetime.utcnow(), "paid_by": by, "payment_method": method}},
        )
        if doc is None:
            return None
        self._refresh_unpaid(doc["user_id"])
        if self.sessions is not None:
            self.sessions.refresh_user(doc["user_id"])
        return doc

    def mark_paid(self, invoice_id: str, method: str, by: str) -> Optional[dict]:
        return self._close_invoice(invoice_id, PAID, method, by)

    def waive(self, invoice_id: str, by: str) -> Optional[dict]:
        return self._close_invoice(invoice_id, WAIVED, "waived", by)

    # ---------- фонова задача ----------

    async def _tick(self) -> None:
        issued = await asyncio.to_thread(self.run_once)
        for invoice in issued:
            if self.sessions is not None:
                self.sessions.refresh_user(invoice["user_id"])
            if self.notify is not None:
                await self.notify(invoice["user_id"], (
                    f"🧾 <b>Рахунок за тиждень: {invoice['amount_usdt']:.2f} USDT</b>\n"
                    f"Чистий прибуток до оплати: {invoice['chargeable']:.2f} USDT, "
                    f"комісія {invoice['fee_percent']:g}%.\n"
                    f"Нові угоди зупинено до оплати. Відкриті позиції супроводжуються далі."
                ))

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[BILLING] tick failed: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
