"""
Залежності ендпоінтів: живі об'єкти бота з app.state (їх проставляє main.py)
і Telegram-авторизація мутуючих запитів. Якщо об'єкта ще немає (бот не
стартував або webapp запущено окремо) — 503, решта API працює далі.
"""

from fastapi import Header, HTTPException, Request

from .auth import validate_init_data


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


def require_telegram_user(x_telegram_init_data: str = Header(default="")) -> dict:
    user = validate_init_data(x_telegram_init_data)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    return user
