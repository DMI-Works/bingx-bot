"""/api/stats, /api/positions, /api/trades — зведення, відкриті позиції, історія."""

import logging
from typing import Optional

from fastapi import APIRouter, Query, Request

from core.common.market_data import extract_account_balance, filter_open_positions, to_float
from core.common.position_metadata import load_metadata

from ..deps import require_db
from ..services.trade_stats import (
    closed_row_to_trade,
    in_period,
    live_margin,
    period_cutoff,
    summarize_closed_trades,
    sum_funding_fees,
    unrealized_pnl,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _fetch_live_snapshot(exchange, cutoff):
    """(unrealized PnL відкритих позицій, equity акаунта, funding усього, funding за період).
    Кожна частина незалежна: недоступність біржі дає нулі/None, а не 5xx."""
    open_unrealized = 0.0
    account_equity: Optional[float] = None
    funding_total = funding_period = 0.0
    if exchange is None:
        return open_unrealized, account_equity, funding_total, funding_period

    try:
        open_unrealized = unrealized_pnl(filter_open_positions(await exchange.get_positions()))
    except Exception as e:
        logger.warning(f"/api/stats: failed to fetch live positions for unrealized PnL: {e}")

    try:
        balance = extract_account_balance(await exchange.get_account_balance())
        if balance is not None:
            account_equity = to_float(balance.get("equity", 0))
    except Exception as e:
        logger.warning(f"/api/stats: failed to fetch account equity: {e}")

    try:
        income_records = await exchange.get_income_history(income_type='FUNDING_FEE', limit=1000)
        funding_total, funding_period = sum_funding_fees(income_records, cutoff)
    except Exception as e:
        logger.warning(f"/api/stats: failed to fetch funding fee history: {e}")

    return open_unrealized, account_equity, funding_total, funding_period


def _pct_of(value: float, base: Optional[float]) -> Optional[float]:
    return round(value / base * 100, 2) if base else None


@router.get("/api/stats")
async def get_stats(request: Request, period: str = Query("1W")):
    db = require_db(request)
    cutoff = period_cutoff(period)

    summary = summarize_closed_trades(db.get_all_closed_positions(), cutoff)
    running = summary.pop("cumulative_pnl_raw")

    open_unrealized, account_equity, funding_total, funding_period = await _fetch_live_snapshot(
        request.app.state.exchange_client, cutoff
    )

    total_pnl = round(running + open_unrealized + funding_total, 2)
    # закриті угоди періоду + фандинг періоду (раніше рахувалось як
    # остання - перша точка кривої і губило першу угоду періоду)
    period_change_usd = round(summary["total_net_pnl"] + funding_period, 2)

    return {
        **summary,
        # equity — накопичений net PnL закритих угод за період (від 0), не баланс акаунта
        "cumulative_pnl": round(running, 2),  # all-time, від period не залежить
        "open_unrealized_pnl": round(open_unrealized, 2),  # знімок "зараз"
        "funding_fees_total": round(funding_total, 2),  # all-time, від'ємне = сплачено біржі
        "funding_fees_period": round(funding_period, 2),
        "total_pnl": total_pnl,  # cumulative_pnl + open_unrealized_pnl + funding_fees_total
        "account_equity": account_equity,  # None, якщо біржа недоступна
        "total_pnl_pct": _pct_of(total_pnl, account_equity),
        "period_change_usd": period_change_usd,
        "period_change_pct": _pct_of(period_change_usd, account_equity),
        "period_pnl_pct": _pct_of(summary["total_net_pnl"], account_equity),
    }


@router.get("/api/positions")
async def get_positions(request: Request):
    """Відкриті позиції: метадані з БД + live-цифри з біржі."""
    db = require_db(request)
    exchange = request.app.state.exchange_client

    db_positions = {(row["symbol"], row["side"]): row for row in db.get_active_positions()}
    if not exchange or not db_positions:
        return []

    try:
        live = await exchange.get_positions()
    except Exception as e:
        logger.error(f"Failed to fetch live positions: {e}", exc_info=True)
        live = []
    live_by_key = {(p.get("symbol"), p.get("positionSide")): p for p in filter_open_positions(live)}

    result = []
    for (symbol, side), row in db_positions.items():
        meta = load_metadata(row["metadata"])
        live_pos = live_by_key.get((symbol, side))

        pnl_usd = float(live_pos["unrealizedProfit"]) if live_pos else None
        margin = live_margin(live_pos) if live_pos else None
        if margin is None:
            margin = meta.get("margin_usdt")

        result.append({
            "order_id": row["order_id"],
            "symbol": symbol,
            "side": side,
            "leverage": meta.get("leverage") or (int(live_pos["leverage"]) if live_pos else None),
            "entry_price": meta.get("entry_price") or (float(live_pos["avgPrice"]) if live_pos else None),
            "mark_price": float(live_pos["markPrice"]) if live_pos else None,
            "stop_loss_price": meta.get("stop_loss_price"),
            "take_profit_levels": meta.get("take_profit_levels"),
            "strategy": meta.get("strategy"),
            "pnl_usd": pnl_usd,
            "pnl_pct": (pnl_usd / margin * 100) if (pnl_usd is not None and margin) else None,
        })

    return result


@router.get("/api/trades")
def get_trades(request: Request, limit: int = 50, offset: int = 0, period: str = Query("ALL")):
    """Пагінована історія закритих угод з урахуванням періоду."""
    db = require_db(request)

    cutoff = period_cutoff(period)
    if cutoff is None:
        rows = db.get_closed_positions(limit=limit, offset=offset)
        total = db.get_closed_positions_count()
    else:
        # фільтр періоду — у Python поверх уже відсортованих (closed_at DESC)
        # закритих позицій, щоб не заводити окремий запит/індекс у БД
        filtered = [r for r in db.get_all_closed_positions() if in_period(r, cutoff)]
        total = len(filtered)
        rows = filtered[offset:offset + limit]

    return {
        "trades": [closed_row_to_trade(r) for r in rows],
        "total": total,
    }
