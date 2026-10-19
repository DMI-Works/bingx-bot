"""
/api/symbols — монети, на які бот підписаний (WS @trade/@depth20), кандидати
на підписку і керування білим/чорним списками (зберігаються в БД через
SettingsManager поверх whitelist/blacklist_symbols з config.yaml).
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from core.common.market_data import held_symbols

from ..deps import (
    require_db,
    require_exchange,
    require_settings_manager,
    require_symbol_selector,
    require_telegram_user,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/symbols")


async def _fetch_held_symbols(exchange, context: str) -> set:
    """Символи з відкритими позиціями — щоб фронт міг пояснити, чому монета
    лишилась у підписці навіть у ЧС (позиція ще супроводжується)."""
    try:
        return held_symbols(await exchange.get_positions())
    except Exception as e:
        logger.error(f"Failed to fetch live positions for {context}: {e}", exc_info=True)
        return set()


def _settings_list(settings_manager, getter: str) -> set:
    return set(getattr(settings_manager, getter)()) if settings_manager else set()


def _subscribed(exchange) -> set:
    return set(getattr(exchange, "subscribed_symbols", set()) or set())


@router.get("")
async def get_symbols(request: Request):
    db = require_db(request)
    exchange = require_exchange(request)
    settings_manager = request.app.state.settings_manager
    signal_tracker = request.app.state.signal_tracker

    subscribed = _subscribed(exchange)
    blacklist = _settings_list(settings_manager, "get_blacklist_symbols")
    held = await _fetch_held_symbols(exchange, "/api/symbols")
    last_traded_by_symbol = db.get_last_position_time_by_symbol()

    result = []
    # ЧС може містити символи, від яких бот уже відписався — показуємо і їх
    for symbol in sorted(subscribed | blacklist):
        last_signal_at = None
        if signal_tracker is not None:
            ts = signal_tracker.last_signal_at(symbol)
            last_signal_at = ts.isoformat() if ts else None

        result.append({
            "symbol": symbol,
            "subscribed": symbol in subscribed,
            "blacklisted": symbol in blacklist,
            "held": symbol in held,
            "last_signal_at": last_signal_at,
            "last_traded_at": last_traded_by_symbol.get(symbol),
        })

    return {"symbols": result}


@router.get("/candidates")
async def get_symbol_candidates(request: Request, limit: int = Query(50, ge=1, le=200)):
    """«Топ монет» так, як їх бачить SymbolSelector: усі тикери біржі з тими
    самими фільтрами (об'єм/спред/ціна), відсортовані за об'ємом. Звідси
    монету можна примусово додати в торгівлю (whitelist)."""
    symbol_selector = require_symbol_selector(request)
    exchange = require_exchange(request)
    settings_manager = request.app.state.settings_manager

    try:
        tickers = await exchange.get_all_tickers()
    except Exception as e:
        logger.error(f"Failed to fetch tickers for /api/symbols/candidates: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Exchange request failed")

    blacklist = _settings_list(settings_manager, "get_blacklist_symbols")
    whitelist = _settings_list(settings_manager, "get_whitelist_symbols")
    subscribed = _subscribed(exchange)
    held = await _fetch_held_symbols(exchange, "/api/symbols/candidates")

    candidates = []
    for ticker in tickers:
        symbol = ticker.get('symbol')
        if not symbol or symbol in blacklist:
            continue
        check = symbol_selector.check_ticker(ticker)
        if check is None:
            continue
        # монету з підписки/whitelist показуємо завжди, навіть якщо зараз вона
        # не пройшла б фільтр — щоб не зникала зі списку на очах у користувача
        if not check.passes and symbol not in (subscribed | whitelist):
            continue

        candidates.append({
            "symbol": symbol,
            "price": check.last_price,
            "volume_24h": check.quote_volume,
            "change_pct": check.change_pct,
            "subscribed": symbol in subscribed,
            "whitelisted": symbol in whitelist,
            "held": symbol in held,
        })

    candidates.sort(key=lambda c: c["volume_24h"], reverse=True)
    return {"candidates": candidates[:limit]}


def _symbol_from_payload(payload: Dict[str, Any]) -> str:
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(status_code=422, detail="'symbol' is required")
    return symbol.strip().upper()


async def _reapply_selection(symbol_selector, action: str) -> None:
    """Застосовує зміну списків одразу: підписує/відписує символи в межах
    цього ж запиту (відписка не чіпає символи з відкритою позицією)."""
    try:
        await symbol_selector.apply()
    except Exception as e:
        logger.error(f"Failed to re-apply symbol selection after {action}: {e}", exc_info=True)


async def _change_list(request: Request, user: dict, symbol: str, list_name: str, add: bool) -> Dict[str, list]:
    settings_manager = require_settings_manager(request)
    symbol_selector = require_symbol_selector(request)

    action = "add" if add else "remove"
    await getattr(settings_manager, f"{action}_{list_name}_symbol")(symbol)
    logger.info(
        f"Symbol '{symbol}' {'added to' if add else 'removed from'} {list_name} via mini app (user={user})"
    )
    await _reapply_selection(symbol_selector, f"{list_name} {action}")
    return {list_name: getattr(settings_manager, f"get_{list_name}_symbols")()}


@router.post("/whitelist")
async def add_symbol_to_whitelist(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    """Примусово додає монету в торгівлю — вона потрапить у підписку при
    найближчому apply(), навіть якщо за об'ємом не входить у топ."""
    return await _change_list(request, user, _symbol_from_payload(payload), "whitelist", add=True)


@router.delete("/whitelist/{symbol}")
async def remove_symbol_from_whitelist(
    symbol: str,
    request: Request,
    user: dict = Depends(require_telegram_user),
):
    return await _change_list(request, user, symbol.strip().upper(), "whitelist", add=False)


@router.post("/blacklist")
async def add_symbol_to_blacklist(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    return await _change_list(request, user, _symbol_from_payload(payload), "blacklist", add=True)


@router.delete("/blacklist/{symbol}")
async def remove_symbol_from_blacklist(
    symbol: str,
    request: Request,
    user: dict = Depends(require_telegram_user),
):
    """Монета знову стає кандидатом при наступному select() — потрапить у
    підписку, лише якщо реально пройде фільтри об'єму/спреду."""
    return await _change_list(request, user, symbol.strip().upper(), "blacklist", add=False)
