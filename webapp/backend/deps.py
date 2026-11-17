"""
Залежності ендпоінтів: живі об'єкти бота з app.state (їх проставляє main.py)
і Telegram-авторизація мутуючих запитів. Якщо об'єкта ще немає (бот не
стартував або webapp запущено окремо) — 503, решта API працює далі.
"""

import logging

from fastapi import Header, HTTPException, Request

from core.common.owner_access import is_owner, owner_ids

from .auth import validate_init_data

logger = logging.getLogger(__name__)


def _require_state(request: Request, attr: str, detail: str):
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise HTTPException(status_code=503, detail=detail)
    return value


def require_db(request: Request):
    return _require_state(request, "db", "Backend not ready yet")


def require_exchange(request: Request):
    return _require_state(request, "exchange_client", "Exchange client not ready")


def require_settings_manager(request: Request):
    return _require_state(request, "settings_manager", "Settings manager not ready yet")


def require_strategy_settings(request: Request):
    return _require_state(request, "strategy_settings", "Strategy settings store not ready yet")


def require_symbol_selector(request: Request):
    return _require_state(request, "symbol_selector", "Symbol selector not ready yet")


def require_trader(request: Request):
    return _require_state(request, "trader", "Trader not ready yet")


def require_restart_event(request: Request):
    # Подія штатного перезапуску (зміна режиму). Є лише коли бот піднятий main.py.
    return _require_state(request, "restart_event", "Bot supervisor not running")


def require_telegram_user(request: Request, x_telegram_init_data: str = Header(default="")) -> dict:
    """Підпис Telegram + власник бота. Висить на КОЖНОМУ роутері (api.py):
    дані й керування реальним рахунком — лише для ADMIN_TELEGRAM_ID."""
    client_ip = request.client.host if request.client else "?"
    user = validate_init_data(x_telegram_init_data)
    if user is None:
        logger.warning(f"[AUTH] 401 invalid init data: {request.method} {request.url.path} ip={client_ip}")
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    if not is_owner(user.get("id")):
        if not owner_ids():
            logger.critical("[AUTH] ADMIN_TELEGRAM_ID не задано — Mini App закритий для всіх")
        logger.warning(
            f"[AUTH] 403 not owner: user={user.get('id')} @{user.get('username')} "
            f"{request.method} {request.url.path} ip={client_ip}"
        )
        raise HTTPException(status_code=403, detail="Доступ только для владельца бота")
    return user
