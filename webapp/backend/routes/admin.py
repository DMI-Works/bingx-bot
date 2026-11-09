"""/api/admin — список користувачів і блокування (лише адмін)."""

import logging
from typing import Any, Dict

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core.database.mongo_config import db_name_for
from core.users import public_view

from ..deps import require_admin, require_billing, require_sessions, require_users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin")


def _closed_pnl_by_user(client, testnet: bool) -> Dict[str, dict]:
    rows = client[db_name_for(testnet)].positions.aggregate([
        {"$match": {"status": "CLOSED", "user_id": {"$exists": True}}},
        {"$group": {
            "_id": "$user_id",
            "net_pnl": {"$sum": {"$ifNull": ["$net_pnl", "$realized_pnl"]}},
            "trades": {"$sum": 1},
        }},
    ])
    return {r["_id"]: r for r in rows}


@router.get("/users")
def list_users(request: Request, admin: dict = Depends(require_admin)):
    users = require_users(request)
    sessions = require_sessions(request)
    client = request.app.state.mongo_client
    live = _closed_pnl_by_user(client, testnet=False)
    testnet = _closed_pnl_by_user(client, testnet=True)

    result = []
    for user in users.list_all():
        session = sessions.get(user["_id"])
        result.append({
            **public_view(user),
            "session_active": session is not None,
            "open_positions": len(session.trader.open_positions) if session is not None else 0,
            "live_net_pnl": round((live.get(user["_id"]) or {}).get("net_pnl") or 0.0, 2),
            "live_trades": (live.get(user["_id"]) or {}).get("trades", 0),
            "testnet_net_pnl": round((testnet.get(user["_id"]) or {}).get("net_pnl") or 0.0, 2),
        })
    return {"users": result}


@router.post("/users/{user_id}/status")
def set_user_status(
    user_id: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    admin: dict = Depends(require_admin),
):
    """Блокування забороняє нові входи. Сесія працює далі: відкриті позиції
    мають супроводжуватись (стоп, фіксація прибутку)."""
    status = payload.get("status")
    if status not in ("active", "blocked"):
        raise HTTPException(status_code=422, detail="'status' must be 'active' or 'blocked'")
    if user_id == admin["_id"]:
        raise HTTPException(status_code=422, detail="Нельзя заблокировать самого себя")
    users = require_users(request)
    if users.get(user_id) is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    users.set_status(user_id, status)
    require_sessions(request).refresh_user(user_id)
    logger.info(f"User {user_id} status -> {status} (admin={admin['_id']})")
    return {"id": user_id, "status": status}


@router.get("/billing")
def get_admin_billing(request: Request, admin: dict = Depends(require_admin)):
    billing = require_billing(request)
    users = {u["_id"]: u for u in require_users(request).list_all()}
    invoices = []
    for invoice in billing.open_invoices():
        user = users.get(invoice["user_id"]) or {}
        invoices.append({**invoice, "username": user.get("username"), "first_name": user.get("first_name")})
    return {**billing.config(), "open_invoices": invoices}


@router.post("/billing/config")
def set_billing_config(request: Request, payload: Dict[str, Any] = Body(...), admin: dict = Depends(require_admin)):
    """Новий відсоток діє на наступні рахунки; уже виставлені не змінюються."""
    value = payload.get("fee_percent")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise HTTPException(status_code=422, detail="Введите процент от 0 до 100")
    config = require_billing(request).set_fee_percent(value)
    logger.info(f"Billing fee set to {value}% (admin={admin['_id']})")
    return config


def _close_invoice(request: Request, invoice_id: str, action: str, admin: dict) -> dict:
    try:
        ObjectId(invoice_id)
    except (InvalidId, TypeError):
        raise HTTPException(status_code=404, detail="Счёт не найден")
    billing = require_billing(request)
    by = str(admin["_id"])
    doc = billing.mark_paid(invoice_id, "manual", by) if action == "paid" else billing.waive(invoice_id, by)
    if doc is None:
        raise HTTPException(status_code=409, detail="Счёт уже закрыт или не найден")
    logger.info(f"Invoice {invoice_id} -> {action} (admin={by})")
    return {"id": invoice_id, "status": action}


@router.post("/invoices/{invoice_id}/paid")
def mark_invoice_paid(invoice_id: str, request: Request, admin: dict = Depends(require_admin)):
    return _close_invoice(request, invoice_id, "paid", admin)


@router.post("/invoices/{invoice_id}/waive")
def waive_invoice(invoice_id: str, request: Request, admin: dict = Depends(require_admin)):
    return _close_invoice(request, invoice_id, "waived", admin)
