"""/api/profile — баланс біржі (live) + режим testnet/live."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.common.market_data import extract_account_balance, to_float

from ..deps import require_session

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/profile")
async def get_profile(session=Depends(require_session)):
    exchange = session.exchange

    try:
        balance_data = await exchange.get_account_balance()
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Exchange request failed")

    b = extract_account_balance(balance_data)
    if b is None:
        raise HTTPException(status_code=502, detail=balance_data.get("msg", "Unknown exchange error"))

    keys = (session.user.get("keys") or {}).get("testnet" if session.testnet else "live") or {}

    return {
        "available": to_float(b.get("availableMargin", 0)),
        "total": to_float(b.get("balance", 0)),
        "unrealized_pnl": to_float(b.get("unrealizedProfit", 0)),
        "used_margin": to_float(b.get("usedMargin", 0)),
        "equity": to_float(b.get("equity", 0)),
        "testnet": session.testnet,
        "api_key_suffix": keys.get("key_suffix"),
    }
