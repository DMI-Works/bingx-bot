"""/api/profile — баланс біржі (live) + режим testnet/live."""

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from core.common.market_data import extract_account_balance, to_float

from ..deps import require_exchange

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/profile")
async def get_profile(request: Request):
    exchange = require_exchange(request)

    try:
        balance_data = await exchange.get_account_balance()
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Exchange request failed")

    b = extract_account_balance(balance_data)
    if b is None:
        raise HTTPException(status_code=502, detail=balance_data.get("msg", "Unknown exchange error"))

    api_key = os.getenv("BINGX_API_KEY", "")

    return {
        "available": to_float(b.get("availableMargin", 0)),
        "total": to_float(b.get("balance", 0)),
        "unrealized_pnl": to_float(b.get("unrealizedProfit", 0)),
        "used_margin": to_float(b.get("usedMargin", 0)),
        "equity": to_float(b.get("equity", 0)),
        "testnet": bool(getattr(exchange, "testnet", True)),
        "api_key_suffix": api_key[-4:] if len(api_key) >= 4 else None,
    }
