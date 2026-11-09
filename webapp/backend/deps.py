"""
Залежності ендпоінтів. Кожен запит авторизується через Telegram initData
(require_user), усі дані рахунку беруться із сесії ЦЬОГО користувача
(require_session), спільні налаштування бота змінює лише адмін (require_admin).
Спільні об'єкти (app.state) проставляє main.py; якщо їх ще немає — 503.
"""

from fastapi import Depends, Header, HTTPException, Request

from .auth import validate_init_data


def _require_state(request: Request, attr: str, detail: str):
    value = getattr(request.app.state, attr, None)
    if value is None:
        raise HTTPException(status_code=503, detail=detail)
    return value


def require_users(request: Request):
    return _require_state(request, "users", "Backend not ready yet")


def require_sessions(request: Request):
    return _require_state(request, "sessions", "Backend not ready yet")


def require_billing(request: Request):
    return _require_state(request, "billing", "Backend not ready yet")


def require_market_exchange(request: Request):
    return _require_state(request, "market_exchange", "Exchange client not ready")


def require_global_settings(request: Request):
    return _require_state(request, "global_settings", "Settings manager not ready yet")


def require_strategy_settings(request: Request):
    return _require_state(request, "strategy_settings", "Strategy settings store not ready yet")


def require_symbol_selector(request: Request):
    return _require_state(request, "symbol_selector", "Symbol selector not ready yet")


def require_user(request: Request, x_telegram_init_data: str = Header(default="")) -> dict:
    """Документ користувача; при першому вході створюється."""
    tg_user = validate_init_data(x_telegram_init_data)
    if tg_user is None or not tg_user.get("id"):
        raise HTTPException(status_code=401, detail="Invalid Telegram init data")
    return require_users(request).ensure_user(tg_user)


def require_session(request: Request, user: dict = Depends(require_user)):
    """Торгова сесія користувача. Немає (ключі не підключено) — 409 no_session,
    Mini App показує екран підключення ключів."""
    session = require_sessions(request).get(user["_id"])
    if session is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_session", "message": "Подключите API-ключи BingX"},
        )
    return session


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Только для администратора")
    return user
