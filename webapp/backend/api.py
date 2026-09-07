# webapp/backend/api.py

import json
import os
import sqlite3
import asyncio
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import validate_init_data
from .bingx_client import BingXClient, BingXAPIError


DB_PATH = Path("../../data/trading_bot.db")
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Ruflo Mini App API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def _load_exchange_config():
    """Load BingX settings from config.yaml, with env vars taking precedence."""
    testnet = os.getenv("BINGX_TESTNET")
    api_key = os.getenv("BINGX_API_KEY")
    api_secret = os.getenv("BINGX_API_SECRET")

    config_candidates = [
        Path(os.getenv("TRADING_BOT_CONFIG", "config.yaml")),
        Path(__file__).resolve().parents[2] / "config.yaml",
        Path(__file__).resolve().parents[3] / "config.yaml",
    ]
    config = {}
    for candidate in config_candidates:
        if candidate.exists():
            try:
                import yaml
                config = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                raise RuntimeError(f"Cannot read config.yaml: {exc}") from exc
            break

    exchange = config.get("exchange", {}) if isinstance(config, dict) else {}
    api_key = api_key or exchange.get("api_key", "")
    api_secret = api_secret or exchange.get("api_secret", "")
    if isinstance(api_key, str) and api_key.startswith("${"):
        api_key = os.getenv(api_key[2:-1], "")
    if isinstance(api_secret, str) and api_secret.startswith("${"):
        api_secret = os.getenv(api_secret[2:-1], "")

    if testnet is None:
        value = exchange.get("testnet", True)
        testnet = str(value).lower() in {"1", "true", "yes", "on"}
    else:
        testnet = str(testnet).lower() in {"1", "true", "yes", "on"}

    if not api_key or not api_secret:
        raise RuntimeError("BingX API credentials are not configured")
    return api_key, api_secret, testnet


_BINGX_CLIENT = None
_BINGX_LOCK = asyncio.Lock()


async def get_bingx_client() -> BingXClient:
    global _BINGX_CLIENT
    if _BINGX_CLIENT is None:
        async with _BINGX_LOCK:
            if _BINGX_CLIENT is None:
                api_key, api_secret, testnet = _load_exchange_config()
                _BINGX_CLIENT = BingXClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    testnet=testnet,
                )
    return _BINGX_CLIENT


@app.on_event("shutdown")
async def shutdown_bingx():
    global _BINGX_CLIENT
    if _BINGX_CLIENT is not None:
        await _BINGX_CLIENT.close()
        _BINGX_CLIENT = None


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def require_telegram_user(
    x_telegram_init_data: str = Header(default="")
) -> dict:
    user = validate_init_data(x_telegram_init_data)

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid Telegram init data",
        )

    return user


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PERIOD_DAYS = {
    "1D": 1,
    "1W": 7,
    "1M": 30,
    "ALL": 3650,
}


def period_days(period: str) -> int:
    return PERIOD_DAYS.get(period.upper(), 7)


def strategy_from_metadata(metadata):
    """
    Достаёт название стратегии из metadata.

    Поддерживает наиболее распространённые варианты:
      {"strategy": "momentum"}
      {"strategy_name": "momentum"}
      {"signal": {"strategy": "momentum"}}

    Если в metadata стратегии нет — возвращаем "Без стратегии".
    """
    if not metadata:
        return "Без стратегии"

    try:
        data = json.loads(metadata) if isinstance(metadata, str) else metadata
    except (TypeError, json.JSONDecodeError):
        return "Без стратегии"

    if not isinstance(data, dict):
        return "Без стратегии"

    value = (
        data.get("strategy")
        or data.get("strategy_name")
        or data.get("strategy_id")
    )

    if isinstance(data.get("signal"), dict):
        value = (
            value
            or data["signal"].get("strategy")
            or data["signal"].get("strategy_name")
        )

    return str(value) if value else "Без стратегии"


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

@app.get("/api/profile")
async def get_profile(
    user: dict = Depends(require_telegram_user),
):
    client = await get_bingx_client()
    try:
        response = await client.get_account_balance()
    except (BingXAPIError, Exception) as exc:
        raise HTTPException(status_code=502, detail=f"BingX balance error: {exc}") from exc

    data = response.get("data", {}) if isinstance(response, dict) else {}
    balance = data.get("balance", data) if isinstance(data, dict) else {}
    return {
        "telegram_user": user,
        "exchange": "BingX",
        "mode": "testnet" if client.testnet else "live",
        "balance": balance,
        "raw": response,
    }


# ---------------------------------------------------------------------------
# Open positions
# ---------------------------------------------------------------------------

@app.get("/api/positions")
async def get_positions(
    user: dict = Depends(require_telegram_user),
):
    client = await get_bingx_client()
    try:
        rows = await client.get_positions()
    except (BingXAPIError, Exception) as exc:
        raise HTTPException(status_code=502, detail=f"BingX positions error: {exc}") from exc

    result = []
    for row in rows or []:
        amount = float(row.get("positionAmt") or row.get("positionAmount") or 0)
        if amount == 0:
            continue
        side = str(row.get("positionSide") or row.get("side") or ("LONG" if amount > 0 else "SHORT")).upper()
        entry = float(row.get("avgPrice") or row.get("entryPrice") or 0)
        mark = float(row.get("markPrice") or 0)
        pnl = float(row.get("unrealizedProfit") or row.get("unrealizedPnl") or 0)
        margin = float(row.get("initialMargin") or row.get("positionInitialMargin") or 0)
        pnl_pct = (pnl / margin * 100) if margin else 0.0
        result.append({
            "symbol": row.get("symbol"),
            "side": side,
            "entry": entry,
            "mark": mark,
            "pnl": pnl,
            "pnl_usdt": pnl,
            "pnl_pct": pnl_pct,
            "sl": None,
            "tp": None,
            "quantity": abs(amount),
            "leverage": row.get("leverage"),
            "margin_usdt": margin,
            "raw": row,
        })
    return result


# ---------------------------------------------------------------------------
# Closed trades
# ---------------------------------------------------------------------------

@app.get("/api/trades")
def get_trades(
    limit: int = Query(20, ge=1, le=200),
    period: str = Query("ALL"),
    user: dict = Depends(require_telegram_user),
):
    days = period_days(period)

    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                id,
                order_id,
                symbol,
                side,
                created_at,
                closed_at,
                close_price,
                realized_pnl,
                roe_percent,
                margin_usdt,
                commission_usdt,
                net_pnl,
                metadata
            FROM positions
            WHERE status = 'CLOSED'
              AND closed_at >= datetime('now', ?)
            ORDER BY closed_at DESC
            LIMIT ?
            """,
            (f"-{days} days", limit),
        ).fetchall()

    result = []

    for row in rows:
        item = dict(row)

        item["pnl"] = item["net_pnl"]
        item["pnl_usdt"] = item["net_pnl"]
        item["pnl_pct"] = item["roe_percent"]
        item["strategy"] = strategy_from_metadata(item["metadata"])

        result.append(item)

    return result


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def get_stats(
    period: str = Query("1W"),
    user: dict = Depends(require_telegram_user),
):
    days = period_days(period)

    with get_db() as db:
        rows = db.execute(
            """
            SELECT
                symbol,
                side,
                closed_at,
                realized_pnl,
                commission_usdt,
                net_pnl,
                roe_percent,
                margin_usdt,
                metadata
            FROM positions
            WHERE status = 'CLOSED'
              AND closed_at >= datetime('now', ?)
            ORDER BY closed_at ASC
            """,
            (f"-{days} days",),
        ).fetchall()

        balance_rows = db.execute(
            """
            SELECT timestamp, total
            FROM balance
            WHERE timestamp >= datetime('now', ?)
            ORDER BY timestamp ASC
            """,
            (f"-{days} days",),
        ).fetchall()

    total_trades = len(rows)

    profitable = sum(
        1 for r in rows
        if (r["net_pnl"] or 0) > 0
    )

    losing = sum(
        1 for r in rows
        if (r["net_pnl"] or 0) < 0
    )

    total_pnl = sum(
        (r["net_pnl"] or 0)
        for r in rows
    )

    win_rate = (
        profitable * 100.0 / total_trades
        if total_trades
        else 0
    )

    # -------------------------------------------------------
    # PnL по монетам
    # -------------------------------------------------------

    coins = {}

    for row in rows:
        symbol = row["symbol"] or "UNKNOWN"
        coins.setdefault(symbol, 0.0)
        coins[symbol] += row["net_pnl"] or 0.0

    pnl_by_symbol = [
        {
            "symbol": symbol,
            "pnl": round(pnl, 8),
        }
        for symbol, pnl in sorted(
            coins.items(),
            key=lambda item: abs(item[1]),
            reverse=True,
        )
    ]

    # -------------------------------------------------------
    # Win/Loss по стратегиям
    # -------------------------------------------------------

    strategies = {}

    for row in rows:
        strategy = strategy_from_metadata(row["metadata"])

        if strategy not in strategies:
            strategies[strategy] = {
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
            }

        pnl = row["net_pnl"] or 0.0

        if pnl > 0:
            strategies[strategy]["wins"] += 1
        elif pnl < 0:
            strategies[strategy]["losses"] += 1

        strategies[strategy]["pnl"] += pnl

    strategy_stats = [
        {
            "strategy": strategy,
            "wins": data["wins"],
            "losses": data["losses"],
            "pnl": round(data["pnl"], 8),
            "trades": data["wins"] + data["losses"],
        }
        for strategy, data in sorted(
            strategies.items(),
            key=lambda item: (
                item[1]["wins"] + item[1]["losses"]
            ),
            reverse=True,
        )
    ]

    return {
        "period": period.upper(),

        "equity": [
            {
                "t": row["timestamp"],
                "v": row["total"],
            }
            for row in balance_rows
        ],

        "summary": {
            "total_trades": total_trades,
            "profitable_count": profitable,
            "losing_count": losing,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 8),
        },

        "pnl_by_symbol": pnl_by_symbol,

        "strategy_stats": strategy_stats,
    }


# ---------------------------------------------------------------------------
# Dedicated endpoints — удобно для отдельных компонентов фронта
# ---------------------------------------------------------------------------

@app.get("/api/analytics/coins")
def get_coin_pnl(
    period: str = Query("1W"),
    user: dict = Depends(require_telegram_user),
):
    stats = get_stats(period=period, user=user)
    return stats["pnl_by_symbol"]


@app.get("/api/analytics/strategies")
def get_strategy_stats(
    period: str = Query("1W"),
    user: dict = Depends(require_telegram_user),
):
    stats = get_stats(period=period, user=user)
    return stats["strategy_stats"]


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount(
        "/",
        StaticFiles(directory=STATIC_DIR, html=True),
        name="static",
    )