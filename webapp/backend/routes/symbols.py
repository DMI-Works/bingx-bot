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
    require_admin,
    require_global_settings,
    require_market_exchange,
    require_session,
    require_sessions,
    require_symbol_selector,
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


def _can_edit_pool(session) -> bool:
    """Звичайний користувач бачить монети, але змінювати їх може лише адмін."""
    return session.user.get("role") == "admin"


def _subscribed(exchange) -> set:
    return set(getattr(exchange, "subscribed_symbols", set()) or set())


@router.get("")
async def get_symbols(request: Request, session=Depends(require_session)):
    """Спільний пул монет + особистий чорний список і позиції користувача."""
    db = session.db
    market = require_market_exchange(request)
    settings_manager = session.settings_manager
    signal_tracker = request.app.state.signal_tracker

    subscribed = _subscribed(market)
    blacklist = _settings_list(settings_manager, "get_blacklist_symbols")
    held = await _fetch_held_symbols(session.exchange, "/api/symbols")
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

    return {"symbols": result, "can_edit_pool": _can_edit_pool(session)}


@router.get("/candidates")
async def get_symbol_candidates(
    request: Request, limit: int = Query(50, ge=1, le=200), session=Depends(require_session),
):
    """«Топ монет» так, як їх бачить SymbolSelector: усі тикери біржі з тими
    самими фільтрами (об'єм/спред/ціна), відсортовані за об'ємом. Звідси
    монету можна примусово додати в торгівлю (whitelist)."""
    symbol_selector = require_symbol_selector(request)
    exchange = require_market_exchange(request)
    global_settings = require_global_settings(request)

    try:
        tickers = await exchange.get_all_tickers()
    except Exception as e:
        logger.error(f"Failed to fetch tickers for /api/symbols/candidates: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail="Exchange request failed")

    blacklist = _settings_list(session.settings_manager, "get_blacklist_symbols")
    whitelist = _settings_list(global_settings, "get_whitelist_symbols")  # спільний пул
    subscribed = _subscribed(exchange)
    held = await _fetch_held_symbols(session.exchange, "/api/symbols/candidates")

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
    return {"candidates": candidates[:limit], "can_edit_pool": _can_edit_pool(session)}


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


async def _reapply_after(request: Request, action: str) -> None:
    await _reapply_selection(require_symbol_selector(request), action)


async def _change_whitelist(request: Request, user: dict, symbol: str, add: bool) -> Dict[str, list]:
    """Whitelist — спільний пул монет усіх користувачів, змінює лише адмін."""
    global_settings = require_global_settings(request)
    action = "add" if add else "remove"
    await getattr(global_settings, f"{action}_whitelist_symbol")(symbol)
    logger.info(f"Symbol '{symbol}' whitelist {action} via mini app (admin={user['_id']})")
    await _reapply_after(request, f"whitelist {action}")
    return {"whitelist": global_settings.get_whitelist_symbols()}


async def _change_blacklist(request: Request, user: dict, symbol: str, add: bool) -> Dict[str, list]:
    """Чорний список адміна: монета прибирається зі спільного пулу для всіх."""
    session = require_sessions(request).get(user["_id"])
    if session is None:
        raise HTTPException(status_code=409, detail={"code": "no_session", "message": "Подключите API-ключи BingX"})
    action = "add" if add else "remove"
    await getattr(session.settings_manager, f"{action}_blacklist_symbol")(symbol)
    await getattr(require_global_settings(request), f"{action}_blacklist_symbol")(symbol)
    await _reapply_after(request, f"blacklist {action}")
    logger.info(f"Symbol '{symbol}' blacklist {action} via mini app (admin={user['_id']})")
    return {"blacklist": session.settings_manager.get_blacklist_symbols()}


@router.post("/whitelist")
async def add_symbol_to_whitelist(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_admin),
):
    """Примусово додає монету в спільний пул при найближчому apply()."""
    return await _change_whitelist(request, user, _symbol_from_payload(payload), add=True)


@router.delete("/whitelist/{symbol}")
async def remove_symbol_from_whitelist(symbol: str, request: Request, user: dict = Depends(require_admin)):
    return await _change_whitelist(request, user, symbol.strip().upper(), add=False)


@router.post("/blacklist")
async def add_symbol_to_blacklist(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_admin),
):
    return await _change_blacklist(request, user, _symbol_from_payload(payload), add=True)


@router.delete("/blacklist/{symbol}")
async def remove_symbol_from_blacklist(symbol: str, request: Request, user: dict = Depends(require_admin)):
    return await _change_blacklist(request, user, symbol.strip().upper(), add=False)
