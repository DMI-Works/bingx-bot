"""
/api/analytics — колекція trade_analytics: повна історія угоди (перенесення
SL, часткові TP, знімок налаштувань стратегії на момент входу). На відміну
від /api/trades (positions — лише фінальний стан), тут можна розібрати КОЖНУ
угоду покроково і порівняти стратегії/монети між собою.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..deps import require_session

router = APIRouter(prefix="/api/analytics")


def _sanitize(doc: dict) -> dict:
    doc = dict(doc)
    doc["_id"] = str(doc["_id"])
    return doc


@router.get("/trades")
def get_analytics_trades(
    symbol: Optional[str] = None,
    strategy: Optional[str] = None,
    mode: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    session=Depends(require_session),
):
    rows = session.db.get_trade_analytics_list(
        symbol=symbol, strategy=strategy, mode=mode, status=status, limit=limit, offset=offset,
    )
    return {"trades": [_sanitize(r) for r in rows]}


@router.get("/trades/{order_id}")
def get_analytics_trade_detail(order_id: str, session=Depends(require_session)):
    row = session.db.get_trade_analytics(order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")
    return _sanitize(row)


@router.get("/summary")
def get_analytics_summary(
    mode: Optional[str] = None, strategy: Optional[str] = None, session=Depends(require_session),
):
    """Агрегація по (strategy, symbol): win-rate, середній net_pnl/ROE%,
    середня кількість переносів SL до закриття."""
    return {"summary": session.db.get_trade_analytics_summary(mode=mode, strategy=strategy)}
