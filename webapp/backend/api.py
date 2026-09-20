"""
Ruflo Mini App backend.

Не открывает свою SQLite-коннекцию и не дублирует схему — использует тот же
объект `Database`, что и TelegramBot, плюс тот же `exchange_client` для
live-данных (открытые позиции, баланс). Оба передаются через app.state из
main.py при старте (см. секцию "Mini App" в main.py).
"""

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .auth import validate_init_data
from core.strategies.param_catalog import catalog_lookup, infer_param_kind

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Ruflo Mini App API")
app.state.db = None
app.state.exchange_client = None
# Опциональны: если main.py их не проставит (webapp.enabled=False сценарий
# сборки/тестов), эндпоинты /api/settings/* просто ответят 503, остальной
# API (stats/positions/trades/profile) продолжит работать как раньше.
app.state.settings_manager = None
app.state.strategy_settings = None
app.state.strategy_manager = None
app.state.symbol_selector = None
app.state.signal_tracker = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
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


PERIOD_TO_DAYS = {"1D": 1, "1W": 7, "1M": 30, "ALL": None}


def _period_cutoff(period: str) -> Optional[datetime]:
    """Единая точка правды для пилюль периода (1D/1W/1M/ALL) — раньше её
    учитывал только /api/stats, а /api/trades отдавал последние N сделок
    вообще без учёта периода."""
    days = PERIOD_TO_DAYS.get(period, 7)
    return datetime.utcnow() - timedelta(days=days) if days is not None else None


def _closed_at_dt(row) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(row["closed_at"]))
    except (TypeError, ValueError, IndexError):
        return None


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

def _profit_factor(gross_profit: float, gross_loss: float) -> Optional[float]:
    """Profit factor = валовая прибыль / валовый убыток (gross_loss — модуль).
    > 1 — стратегия в плюсе, < 1 — в минусе, вне зависимости от win rate.
    None, если убытков не было (делить не на что) — фронт покажет «∞»/«—»."""
    if gross_loss <= 0:
        return None
    return round(gross_profit / gross_loss, 2)


@app.get("/api/stats")
def get_stats(request: Request, period: str = Query("1W")):  
    db = _require_deps(request)

    all_closed = db.get_all_closed_positions()  # ORDER BY closed_at DESC
    rows = list(reversed(all_closed))  # хронологически, для накопительной суммы

    cutoff = _period_cutoff(period)

    equity = []
    running = 0.0  
    symbol_pnl: dict = defaultdict(float)
    # name -> счётчики W/L + деньги (для profit factor и PnL по стратегии)
    strategy_stats_acc: dict = defaultdict(
        lambda: {"win": 0, "loss": 0, "pnl": 0.0, "gross_profit": 0.0, "gross_loss": 0.0}
    )

    total_trades = 0
    winning = 0
    losing = 0
    breakeven = 0
    gross_profit = 0.0   # сумма всех прибыльных сделок (net)
    gross_loss = 0.0     # сумма модулей всех убыточных сделок (net)
    best_trade: Optional[float] = None
    worst_trade: Optional[float] = None
    total_net_pnl = 0.0
    total_commission_usdt = 0.0

    for row in rows:
        net = row["net_pnl"] if row["net_pnl"] is not None else (row["realized_pnl"] or 0.0)
        running += net

        closed_at_raw = row["closed_at"]
        try:
            closed_at = datetime.fromisoformat(str(closed_at_raw))
        except (TypeError, ValueError, IndexError):
            closed_at = None

        in_period = cutoff is None or (closed_at and closed_at >= cutoff)

        if in_period:
            equity.append({"t": str(closed_at_raw), "v": round(running, 2)})

            meta = _parse_metadata(row)
            strategy_name = meta.get("strategy") or "Невідомо"
            acc = strategy_stats_acc[strategy_name]
            acc["win" if net >= 0 else "loss"] += 1
            acc["pnl"] += net
            if net > 0:
                acc["gross_profit"] += net
            elif net < 0:
                acc["gross_loss"] += -net
            symbol_pnl[row["symbol"]] += net

            total_trades += 1
            if net > 0:
                winning += 1
                gross_profit += net
            elif net < 0:
                losing += 1
                gross_loss += -net
            else:
                breakeven += 1
            best_trade = net if best_trade is None else max(best_trade, net)
            worst_trade = net if worst_trade is None else min(worst_trade, net)
            total_net_pnl += net
            commission = row["commission_usdt"] or 0.0
            total_commission_usdt += commission

    win_rate = round((winning / total_trades) * 100, 1) if total_trades else 0.0

    # Прибыльность: win rate сам по себе обманчив (5 вин по $0.20 и один луз
    # на $4 = 83% win rate, но минус), поэтому считаем ещё деньги.
    avg_win = gross_profit / winning if winning else None
    avg_loss = gross_loss / losing if losing else None   # модуль (положительное число)
    payoff_ratio = round(avg_win / avg_loss, 2) if avg_win is not None and avg_loss else None

    return {
        "equity": equity,  # кумулятивный net PnL закрытых сделок, не полный баланс аккаунта
        "cumulative_pnl": round(running, 2),
        "win_rate": win_rate,
        "total_trades": total_trades,
        "total_net_pnl": round(total_net_pnl, 2),
        "total_commission_usdt": round(total_commission_usdt, 2),
        "winning_trades": winning,
        "losing_trades": losing,
        "breakeven_trades": breakeven,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),          # модуль
        "profit_factor": _profit_factor(gross_profit, gross_loss),  # None, если нет убытков
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,  # модуль
        "payoff_ratio": payoff_ratio,                # средний win / средний loss
        "best_trade": round(best_trade, 2) if best_trade is not None else None,
        "worst_trade": round(worst_trade, 2) if worst_trade is not None else None,
        "symbol_pnl": [{"symbol": s, "pnl": round(v, 2)} for s, v in sorted(symbol_pnl.items(), key=lambda kv: abs(kv[1]), reverse=True)],
        "strategy_stats": [
            {
                "strategy": name,
                "win": a["win"],
                "loss": a["loss"],
                "pnl": round(a["pnl"], 2),
                "profit_factor": _profit_factor(a["gross_profit"], a["gross_loss"]),
            }
            for name, a in strategy_stats_acc.items()
        ],
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
       
        margin = None
        if live_pos:
            for key in ("isolatedMargin", "initialMargin", "margin", "positionMargin"):
                if key in live_pos and live_pos[key] not in (None, ""):
                    try:
                        margin = float(live_pos[key])
                    except (TypeError, ValueError):
                        continue
                    break
        if margin is None:
            margin = meta.get("margin_usdt")
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
def get_trades(request: Request, limit: int = 50, offset: int = 0, period: str = Query("ALL")):
    db = _require_deps(request)

    cutoff = _period_cutoff(period)
    if cutoff is None:
        # ALL — прежнее поведение, простая пагинация на уровне БД.
        rows = db.get_closed_positions(limit=limit, offset=offset)
        total = db.get_closed_positions_count()
    else:
        # Пилюли периода (1D/1W/1M) раньше никак не влияли на историю
        # сделок — только на график/сводку в /api/stats. Фильтруем здесь
        # так же, как там: берём все закрытые позиции (уже ORDER BY
        # closed_at DESC) и отсекаем по cutoff в Python, чтобы не трогать
        # схему БД ради ещё одного индекса/запроса.
        filtered = [r for r in db.get_all_closed_positions() if (_closed_at_dt(r) or datetime.min) >= cutoff]
        total = len(filtered)
        rows = filtered[offset:offset + limit]

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

    api_key = os.getenv("BINGX_API_KEY", "")
    api_key_suffix = api_key[-4:] if len(api_key) >= 4 else None

    return {
        "available": float(b.get("availableMargin", 0)),
        "total": float(b.get("balance", 0)),
        "unrealized_pnl": float(b.get("unrealizedProfit", 0)),
        "used_margin": float(b.get("usedMargin", 0)),
        "equity": float(b.get("equity", 0)),
        "testnet": bool(getattr(exchange, "testnet", True)),
        "api_key_suffix": api_key_suffix,
    }


# ---------------------------------------------------------------------------
# /api/settings — глобальный тумблер торговли + список стратегий с их
# параметрами. Тонкая HTTP-обёртка поверх уже существующей логики:
#   - core/state/settings_manager.py       -> trading.enabled (kill-switch)
#   - core/database/strategy_settings.py   -> StrategySettingsStore (БД)
#   - core/strategies/strategies_setup.py  -> StrategyManager (live-инстансы)
# Ничего не решает и не пересчитывает сама — только читает/пишет то, что уже
# считает бэкенд бота. Мутирующие эндпоинты требуют Telegram-авторизации,
# т.к. управляют реальной торговлей.
# ---------------------------------------------------------------------------

def _require_settings_manager(request: Request):
    settings_manager = request.app.state.settings_manager
    if settings_manager is None:
        raise HTTPException(status_code=503, detail="Settings manager not ready yet")
    return settings_manager


def _require_strategy_settings(request: Request):
    strategy_settings = request.app.state.strategy_settings
    if strategy_settings is None:
        raise HTTPException(status_code=503, detail="Strategy settings store not ready yet")
    return strategy_settings


def _serialize_strategy(entry: Dict[str, Any], store) -> Dict[str, Any]:
    name = entry["strategy_name"]
    params = []
    for key, value in entry["params"].items():
        label, description = catalog_lookup(key)
        params.append({
            "key": key,
            "label": label,
            "description": description,
            "kind": infer_param_kind(value) or "text",
            "value": value,
        })
    return {
        "name": name,
        "enabled": entry["enabled"],
        "modified": store.is_modified(name),
        "updated_at": entry["updated_at"],
        "params": params,
    }


def _apply_params_live(request: Request, name: str, params: Dict[str, Any]) -> None:
    """Если рядом есть live StrategyManager — применяет новые параметры к
    работающему инстансу стратегии сразу, без рестарта бота (см.
    StrategyManager.apply_params). Если его нет (например, локальный запуск
    только веб-части без main.py) — изменения всё равно сохранены в БД и
    подхватятся при следующем старте."""
    strategy_manager = request.app.state.strategy_manager
    if strategy_manager is not None:
        strategy_manager.apply_params(name, params)


@app.get("/api/settings")
def get_settings(request: Request):  # noqa: ARG001 — добавьте Depends(require_telegram_user) для прода
    settings_manager = _require_settings_manager(request)
    store = _require_strategy_settings(request)

    strategies = [_serialize_strategy(entry, store) for entry in store.list_strategies()]

    return {
        "trading_enabled": settings_manager.get_trading_enabled(),
        "strategies": strategies,
    }


@app.post("/api/settings/trading")
async def set_trading_enabled(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    settings_manager = _require_settings_manager(request)
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    await settings_manager.set_trading_enabled(enabled)
    logger.info(f"Trading globally {'enabled' if enabled else 'disabled'} via mini app (user={user})")
    return {"trading_enabled": enabled}


@app.post("/api/settings/strategies/{name}/enabled")
async def set_strategy_enabled(
    name: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    store = _require_strategy_settings(request)
    if store.get_params(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown strategy '{name}'")

    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    strategy_manager = request.app.state.strategy_manager
    if strategy_manager is not None:
        strategy_manager.set_enabled(name, enabled)  # обновляет и БД, и live-инстанс
    else:
        store.set_enabled(name, enabled)

    logger.info(f"Strategy '{name}' {'enabled' if enabled else 'disabled'} via mini app (user={user})")

    entries = {e["strategy_name"]: e for e in store.list_strategies()}
    return _serialize_strategy(entries[name], store)


@app.post("/api/settings/strategies/{name}/params")
async def update_strategy_param(
    name: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    store = _require_strategy_settings(request)
    current = store.get_params(name)
    if current is None:
        raise HTTPException(status_code=404, detail=f"Unknown strategy '{name}'")

    key = payload.get("key")
    if not isinstance(key, str) or key not in current:
        raise HTTPException(status_code=422, detail=f"Unknown param key for strategy '{name}'")
    if "value" not in payload:
        raise HTTPException(status_code=422, detail="'value' is required")

    current[key] = payload["value"]
    store.update_params(name, current)
    _apply_params_live(request, name, current)

    logger.info(f"Strategy '{name}' param '{key}' updated via mini app (user={user})")

    entries = {e["strategy_name"]: e for e in store.list_strategies()}
    return _serialize_strategy(entries[name], store)


@app.post("/api/settings/strategies/{name}/reset")
async def reset_strategy_params(
    name: str,
    request: Request,
    user: dict = Depends(require_telegram_user),
):
    store = _require_strategy_settings(request)
    if store.get_params(name) is None:
        raise HTTPException(status_code=404, detail=f"Unknown strategy '{name}'")

    reset_params = store.reset_to_default(name)
    _apply_params_live(request, name, reset_params)

    logger.info(f"Strategy '{name}' reset to defaults via mini app (user={user})")

    entries = {e["strategy_name"]: e for e in store.list_strategies()}
    return _serialize_strategy(entries[name], store)


# ---------------------------------------------------------------------------
# /api/symbols — монеты, на которые бот сейчас подписан (WS @trade/@depth20),
# + управление чёрным списком (хранится в БД через SettingsManager, поверх
# blacklist_symbols из config.yaml — см. SymbolSelector.select()).
# ---------------------------------------------------------------------------

@app.get("/api/symbols")
async def get_symbols(request: Request):
    exchange = request.app.state.exchange_client
    settings_manager = request.app.state.settings_manager
    signal_tracker = request.app.state.signal_tracker
    db = _require_deps(request)

    if exchange is None:
        raise HTTPException(status_code=503, detail="Exchange client not ready")

    subscribed = sorted(getattr(exchange, "subscribed_symbols", set()) or set())
    blacklist = set(settings_manager.get_blacklist_symbols()) if settings_manager else set()

    # held (открытые позиции) — чтобы во фронте можно было объяснить, почему
    # монета осталась в списке, даже если добавлена в ЧС (позиция всё ещё
    # сопровождается, см. SymbolSelector._get_held_symbols)
    held_symbols = set()
    try:
        live_positions = await exchange.get_positions()
        held_symbols = {
            p.get("symbol") for p in live_positions
            if float(p.get("positionAmt", 0)) != 0
        }
    except Exception as e:
        logger.error(f"Failed to fetch live positions for /api/symbols: {e}", exc_info=True)

    last_traded_by_symbol = db.get_last_position_time_by_symbol()

    # чёрный список может содержать символы, на которые бот уже не подписан
    # (их уже отписали) — показываем их тоже, отдельным списком
    all_symbols = sorted(set(subscribed) | blacklist)

    result = []
    for symbol in all_symbols:
        last_signal_at = None
        if signal_tracker is not None:
            ts = signal_tracker.last_signal_at(symbol)
            last_signal_at = ts.isoformat() if ts else None

        result.append({
            "symbol": symbol,
            "subscribed": symbol in subscribed,
            "blacklisted": symbol in blacklist,
            "held": symbol in held_symbols,
            "last_signal_at": last_signal_at,
            "last_traded_at": last_traded_by_symbol.get(symbol),
        })

    return {"symbols": result}


def _require_symbol_selector(request: Request):
    symbol_selector = request.app.state.symbol_selector
    if symbol_selector is None:
        raise HTTPException(status_code=503, detail="Symbol selector not ready yet")
    return symbol_selector


@app.post("/api/symbols/blacklist")
async def add_symbol_to_blacklist(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    settings_manager = _require_settings_manager(request)
    symbol_selector = _require_symbol_selector(request)

    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(status_code=422, detail="'symbol' is required")
    symbol = symbol.strip().upper()

    await settings_manager.add_blacklist_symbol(symbol)
    logger.info(f"Symbol '{symbol}' added to blacklist via mini app (user={user})")

    # применяем сразу — отпишет символ от WS в течение этого вызова, если
    # он не держит открытую позицию (см. SymbolSelector._get_held_symbols)
    try:
        await symbol_selector.apply()
    except Exception as e:
        logger.error(f"Failed to re-apply symbol selection after blacklist add: {e}", exc_info=True)

    return {"blacklist": settings_manager.get_blacklist_symbols()}


@app.delete("/api/symbols/blacklist/{symbol}")
async def remove_symbol_from_blacklist(
    symbol: str,
    request: Request,
    user: dict = Depends(require_telegram_user),
):
    settings_manager = _require_settings_manager(request)
    symbol_selector = _require_symbol_selector(request)

    symbol = symbol.strip().upper()
    await settings_manager.remove_blacklist_symbol(symbol)
    logger.info(f"Symbol '{symbol}' removed from blacklist via mini app (user={user})")

    # применяем сразу — символ снова становится кандидатом при следующем
    # select() (попадёт в подписку, только если реально пройдёт фильтры
    # объёма/спреда, а не мгновенно принудительно)
    try:
        await symbol_selector.apply()
    except Exception as e:
        logger.error(f"Failed to re-apply symbol selection after blacklist remove: {e}", exc_info=True)

    return {"blacklist": settings_manager.get_blacklist_symbols()}


# ---------------------------------------------------------------------------
# Отдаём собранный фронт (npm run build кладёт файлы в static/)
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")