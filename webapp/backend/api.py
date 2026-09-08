"""
Ruflo Mini App backend.

Не открывает свою SQLite-коннекцию и не дублирует схему — использует тот же
объект `Database`, что и TelegramBot, плюс тот же `exchange_client` для
live-данных (открытые позиции, баланс). Оба передаются через app.state из
main.py при старте (см. секцию "Mini App" в main.py).
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .auth import validate_init_data

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Ruflo Mini App API")
app.state.db = None
app.state.exchange_client = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _require_deps(request: Request):
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="Backend not ready yet")
    return db


def require_telegram_user(x_telegram_init_data: str = Header(default="")) -> dict:
    user = validate_init_data(x_telegram_init_data)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    return user


def _parse_metadata(row) -> dict:
    try:
        return json.loads(row["metadata"]) if row["metadata"] else {}
    except (TypeError, ValueError):
        return {}


def _closed_row_to_trade(row) -> dict:
    meta = _parse_metadata(row)
    net_pnl = row["net_pnl"] if row["net_pnl"] is not None else row["realized_pnl"]
    return {
        "order_id": row["order_id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "leverage": meta.get("leverage"),
        "entry_price": meta.get("entry_price"),
        "close_price": row["close_price"],
        "stop_loss_price": meta.get("stop_loss_price"),
        "take_profit_levels": meta.get("take_profit_levels"),
        "strategy": meta.get("strategy"),
        "margin_usdt": row["margin_usdt"],
        "commission_usdt": row["commission_usdt"],
        "net_pnl": net_pnl,
        "roe_percent": row["roe_percent"],
        "closed_at": row["closed_at"],
    }


# ---------------------------------------------------------------------------
# /api/stats — сводка + кривая кумулятивного PnL за период
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def get_stats(request: Request, period: str = Query("1W")):  # noqa: ARG001 — добавьте Depends(require_telegram_user) для прода
    db = _require_deps(request)

    summary = db.get_stats_summary()
    total_trades = summary.get("total_trades") or 0
    winning = summary.get("winning_trades") or 0

    period_to_days = {"1D": 1, "1W": 7, "1M": 30, "ALL": None}
    days = period_to_days.get(period, 7)

    all_closed = db.get_all_closed_positions()  # ORDER BY closed_at DESC
    rows = list(reversed(all_closed))  # хронологически, для накопительной суммы

    if days is not None:
        cutoff = datetime.utcnow() - timedelta(days=days)
    else:
        cutoff = None

    equity = []
    running = 0.0
    symbol_pnl: dict = defaultdict(float)
    strategy_counts: dict = defaultdict(lambda: [0, 0])  # name -> [win, loss]

    for row in rows:
        net = row["net_pnl"] if row["net_pnl"] is not None else (row["realized_pnl"] or 0.0)
        running += net

        closed_at_raw = row["closed_at"]
        try:
            closed_at = datetime.fromisoformat(str(closed_at_raw))
        except (TypeError, ValueError):
            closed_at = None

        meta = _parse_metadata(row)
        strategy_name = meta.get("strategy") or "Невідомо"
        counts = strategy_counts[strategy_name]
        counts[0 if net >= 0 else 1] += 1
        symbol_pnl[row["symbol"]] += net

        if cutoff is None or (closed_at and closed_at >= cutoff):
            equity.append({"t": str(closed_at_raw), "v": round(running, 2)})

    win_rate = round((winning / total_trades) * 100, 1) if total_trades else 0.0

    return {
        "equity": equity,  # кумулятивный net PnL закрытых сделок, не полный баланс аккаунта
        "cumulative_pnl": round(running, 2),
        "win_rate": win_rate,
        "total_trades": total_trades,
        "total_net_pnl": round(summary.get("total_net_pnl") or 0.0, 2),
        "total_commission_usdt": round(summary.get("total_commission_usdt") or 0.0, 2),
        "symbol_pnl": [{"symbol": s, "pnl": round(v, 2)} for s, v in sorted(symbol_pnl.items(), key=lambda kv: abs(kv[1]), reverse=True)],
        "strategy_stats": [{"strategy": name, "win": w, "loss": l} for name, (w, l) in strategy_counts.items()],
    }


# ---------------------------------------------------------------------------
# /api/positions — открытые позиции: метаданные из БД + live-цифры с биржи
# ---------------------------------------------------------------------------

@app.get("/api/positions")
async def get_positions(request: Request):
    db = _require_deps(request)
    exchange = request.app.state.exchange_client

    db_positions = {(row["symbol"], row["side"]): row for row in db.get_active_positions()}

    if not exchange or not db_positions:
        # Нет открытых позиций в БД, либо биржевой клиент ещё не готов
        return []

    try:
        live = await exchange.get_positions()
    except Exception as e:
        logger.error(f"Failed to fetch live positions: {e}", exc_info=True)
        live = []

    live_by_key = {}
    for pos in live:
        if float(pos.get("positionAmt", 0)) == 0:
            continue
        live_by_key[(pos.get("symbol"), pos.get("positionSide"))] = pos

    result = []
    for (symbol, side), row in db_positions.items():
        meta = _parse_metadata(row)
        live_pos = live_by_key.get((symbol, side))

        entry_price = meta.get("entry_price") or (float(live_pos["avgPrice"]) if live_pos else None)
        mark_price = float(live_pos["markPrice"]) if live_pos else None
        pnl_usd = float(live_pos["unrealizedProfit"]) if live_pos else None
        margin = float(live_pos["isolatedMargin"]) if live_pos else meta.get("margin_usdt")
        pnl_pct = (pnl_usd / margin * 100) if (pnl_usd is not None and margin) else None

        result.append({
            "order_id": row["order_id"],
            "symbol": symbol,
            "side": side,
            "leverage": meta.get("leverage") or (int(live_pos["leverage"]) if live_pos else None),
            "entry_price": entry_price,
            "mark_price": mark_price,
            "stop_loss_price": meta.get("stop_loss_price"),
            "take_profit_levels": meta.get("take_profit_levels"),
            "strategy": meta.get("strategy"),
            "pnl_usd": pnl_usd,
            "pnl_pct": pnl_pct,
        })

    return result


# ---------------------------------------------------------------------------
# /api/trades — пагинированная история закрытых сделок
# ---------------------------------------------------------------------------

@app.get("/api/trades")
def get_trades(request: Request, limit: int = 20, offset: int = 0):
    db = _require_deps(request)
    rows = db.get_closed_positions(limit=limit, offset=offset)
    total = db.get_closed_positions_count()
    return {
        "trades": [_closed_row_to_trade(r) for r in rows],
        "total": total,
    }


# ---------------------------------------------------------------------------
# /api/profile — баланс биржи (live) + режим testnet/live
# ---------------------------------------------------------------------------

@app.get("/api/profile")
async def get_profile(request: Request):
    exchange = request.app.state.exchange_client
    if not exchange:
        raise HTTPException(status_code=503, detail="Exchange client not ready")

    try:
        balance_data = await exchange.get_account_balance()
    except Exception as e:
        logger.error(f"Failed to fetch balance: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Exchange request failed")

    if balance_data.get("code") != 0 or "data" not in balance_data:
        raise HTTPException(status_code=502, detail=balance_data.get("msg", "Unknown exchange error"))

    b = balance_data["data"].get("balance", {})
    return {
        "available": float(b.get("availableMargin", 0)),
        "total": float(b.get("balance", 0)),
        "unrealized_pnl": float(b.get("unrealizedProfit", 0)),
        "used_margin": float(b.get("usedMargin", 0)),
        "equity": float(b.get("equity", 0)),
    }


# ---------------------------------------------------------------------------
# Отдаём собранный фронт (npm run build кладёт файлы в static/)
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
