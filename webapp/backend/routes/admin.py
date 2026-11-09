"""/api/admin — список користувачів і блокування (лише адмін)."""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core.database.mongo_config import db_name_for
from core.users import public_view

from ..deps import require_admin, require_sessions, require_users

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
