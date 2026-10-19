"""
/api/analytics — колекція trade_analytics: повна історія угоди (перенесення
SL, часткові TP, знімок налаштувань стратегії на момент входу). На відміну
від /api/trades (positions — лише фінальний стан), тут можна розібрати КОЖНУ
угоду покроково і порівняти стратегії/монети між собою.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ..deps import require_db

router = APIRouter(prefix="/api/analytics")


def _sanitize(doc: dict) -> dict:
    doc = dict(doc)
    doc["_id"] = str(doc["_id"])
    return doc


@router.get("/trades")
def get_analytics_trades(
    request: Request,
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    mode: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    rows = require_db(request).get_trade_analytics_list(
        symbol=symbol, strategy=strategy, mode=mode, status=status, limit=limit, offset=offset,
    )
    return {"trades": [_sanitize(r) for r in rows]}


@router.get("/trades/{order_id}")
def get_analytics_trade_detail(request: Request, order_id: str):
    row = require_db(request).get_trade_analytics(order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")
    return _sanitize(row)


@router.get("/summary")
def get_analytics_summary(request: Request, mode: Optional[str] = None, strategy: Optional[str] = None):
    """Агрегація по (strategy, symbol): win-rate, середній net_pnl/ROE%,
    середня кількість переносів SL до закриття."""
    return {"summary": require_db(request).get_trade_analytics_summary(mode=mode, strategy=strategy)}
