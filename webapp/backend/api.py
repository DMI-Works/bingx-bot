"""
Ruflo Mini App backend.

Тонкий read-only слой поверх той же SQLite базы, что использует торговый бот
(core/database). Ничего не пишет в БД и не трогает торговую логику — только
отдаёт данные для мини-аппа.

Запуск отдельно (для разработки):
    uvicorn webapp.backend.api:app --reload --port 8000

Запуск вместе с ботом — см. пример в main.py в конце этого файла.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import validate_init_data

DB_PATH = Path("data/trading_bot.db")  # та же база, что у бота
STATIC_DIR = Path(__file__).parent / "static"  # сюда собирается фронт (vite build)

app = FastAPI(title="Ruflo Mini App API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Telegram сам открывает WebApp в своём webview, ужесточите при необходимости
    allow_methods=["GET"],
    allow_headers=["*"],
)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def require_telegram_user(x_telegram_init_data: str = Header(default="")) -> dict:
    """
    Проверяет подпись initData, которую Telegram Mini App передаёт на каждый
    запрос. Без этого кто угодно с прямой ссылкой на API увидит вашу
    статистику. См. auth.py.
    """
    user = validate_init_data(x_telegram_init_data)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    return user


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def get_stats(period: str = Query("1W"), user=None):  # noqa: ARG001 — заменить на Depends(require_telegram_user)
    """
    Кривая equity + сводные метрики за период.
    TODO: замените запросы под реальную схему core/database (таблицы balance,
    trades, positions — уточните точные имена колонок в core/database/models.py).
    """
    period_to_days = {"1D": 1, "1W": 7, "1M": 30, "ALL": 3650}
    days = period_to_days.get(period, 7)

    with get_db() as db:
        equity_rows = db.execute(
            """
            SELECT timestamp, balance
            FROM balance_history
            WHERE timestamp >= datetime('now', ?)
            ORDER BY timestamp ASC
            """,
            (f"-{days} days",),
        ).fetchall()

        summary = db.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE pnl > 0) * 100.0 / NULLIF(COUNT(*), 0) AS win_rate,
                COUNT(*) AS total_trades
            FROM trades
            WHERE closed_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        ).fetchone()

    return {
        "equity": [{"t": r["timestamp"], "v": r["balance"]} for r in equity_rows],
        "win_rate": round(summary["win_rate"] or 0, 1),
        "total_trades": summary["total_trades"] or 0,
    }


@app.get("/api/positions")
def get_positions(user=None):  # noqa: ARG001
    with get_db() as db:
        rows = db.execute(
            """
            SELECT symbol, side, entry_price, mark_price, pnl_usd, pnl_pct,
                   stop_loss, take_profit
            FROM positions
            WHERE status = 'OPEN'
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/trades")
def get_trades(limit: int = 20, user=None):  # noqa: ARG001
    with get_db() as db:
        rows = db.execute(
            """
            SELECT symbol, side, closed_at, pnl_usd, pnl_pct
            FROM trades
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/profile")
def get_profile(user=None):  # noqa: ARG001
    with get_db() as db:
        balance = db.execute(
            "SELECT available, in_positions, total FROM balance ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    return {
        "exchange": "BingX",
        "mode": "testnet",  # читайте из config/config.yaml
        "balance": dict(balance) if balance else None,
    }


# ---------------------------------------------------------------------------
# Отдаём собранный фронт (npm run build кладёт файлы в static/)
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
