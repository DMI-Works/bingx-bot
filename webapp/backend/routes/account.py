"""/api/me і /api/account/keys — поточний користувач і його API-ключі BingX."""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core.common.market_data import extract_account_balance
from core.exchange import BingXClient
from core.exchange.errors import BingXAPIError
from core.users import SecretsKeyMissing, public_view
from core.users.access import can_open_new

from ..deps import limit_keys, require_billing, require_sessions, require_user, require_users

logger = logging.getLogger(__name__)

router = APIRouter()


def _me(request: Request, user: dict) -> Dict[str, Any]:
    allowed, reason = can_open_new(user)
    return {
        **public_view(user),
        "session_active": require_sessions(request).get(user["_id"]) is not None,
        "can_open": allowed,
        "can_open_reason": reason,
    }


@router.get("/api/me")
def get_me(request: Request, user: dict = Depends(require_user)):
    return _me(request, user)


@router.get("/api/billing")
def get_billing(request: Request, user: dict = Depends(require_user)):
    return require_billing(request).user_status(user)


def _key_field(payload: Dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise HTTPException(status_code=422, detail=f"'{name}' is required")
    return value.strip()


# коди прав ключа BingX (GET /openApi/v1/account/apiPermissions)
PERMISSION_FUTURES = 3
PERMISSION_WITHDRAW = 5


def check_key_permissions(perms: Dict[str, Any]) -> None:
    """Ключ із правом виведення не приймаємо: при витоку бази разом із
    SECRETS_KEY ним можна вивести гроші користувача. Без ф'ючерсів бот не
    зможе торгувати — кажемо одразу, а не при першій угоді."""
    codes = set(perms.get("permissions") or [])
    withdraw_flags = [k for k, v in perms.items() if "withdraw" in k.lower() and v is True]
    if PERMISSION_WITHDRAW in codes or withdraw_flags:
        raise HTTPException(
            status_code=400,
            detail="У ключа включён вывод средств — создайте ключ без права «Вывод» (Withdraw)",
        )
    if PERMISSION_FUTURES not in codes and perms.get("enableFutures") is not True:
        raise HTTPException(
            status_code=400,
            detail="У ключа нет права торговли фьючерсами — включите «Бессрочные фьючерсы» при создании ключа",
        )


async def _verify_keys(api_key: str, api_secret: str, testnet: bool) -> None:
    """Ключ зберігаємо лише якщо біржа реально його прийняла і в нього немає
    права виведення коштів."""
    client = BingXClient(api_key=api_key, api_secret=api_secret, testnet=testnet)
    try:
        balance = await client.get_account_balance()
        perms = await client.get_api_permissions()
    except BingXAPIError as e:
        raise HTTPException(status_code=400, detail=f"Биржа отклонила ключ: {e.code} {e.msg}")
    except Exception as e:
        logger.error(f"Key verification request failed: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Не удалось связаться с биржей")
    finally:
        await client.close()
    if extract_account_balance(balance) is None:
        raise HTTPException(status_code=400, detail="Биржа не вернула баланс по этому ключу")
    check_key_permissions(perms)


def _guard_open_positions(session, testnet: bool) -> None:
    # новий ключ може бути від іншого рахунку — відкриті позиції лишились би без супроводу
    if session is not None and session.testnet == testnet and session.trader.open_positions:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "open_positions",
                "message": "Есть открытые позиции — сначала закройте их",
                "open_positions": len(session.trader.open_positions),
            },
        )


@router.post("/api/account/keys", dependencies=[Depends(limit_keys)])
async def save_keys(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_user),
):
    users = require_users(request)
    sessions = require_sessions(request)
    testnet = payload.get("testnet")
    if not isinstance(testnet, bool):
        raise HTTPException(status_code=422, detail="'testnet' must be a boolean")
    api_key, api_secret = _key_field(payload, "api_key"), _key_field(payload, "api_secret")

    _guard_open_positions(sessions.get(user["_id"]), testnet)
    await _verify_keys(api_key, api_secret, testnet)
    try:
        users.save_keys(user["_id"], testnet, api_key, api_secret, can_withdraw=False)  # перевірено в _verify_keys
    except SecretsKeyMissing:
        logger.critical("SECRETS_KEY is not set — cannot store user API keys")
        raise HTTPException(status_code=503, detail="Сервис временно недоступен")

    # перший ключ визначає режим рахунку, якщо для поточного режиму ключів немає
    fresh = users.get(user["_id"])
    if not users.has_keys(fresh, bool(fresh.get("testnet", True))):
        users.set_testnet(user["_id"], testnet)
        fresh = users.get(user["_id"])

    if bool(fresh.get("testnet", True)) == testnet:
        if await sessions.start_user(user["_id"]) is None:
            raise HTTPException(status_code=502, detail="Ключ сохранён, но запустить торговлю не удалось")
    logger.info(f"API keys updated for user {user['_id']} ({'testnet' if testnet else 'live'})")
    return _me(request, users.get(user["_id"]))


@router.delete("/api/account/keys", dependencies=[Depends(limit_keys)])
async def delete_keys(request: Request, testnet: bool, user: dict = Depends(require_user)):
    users = require_users(request)
    sessions = require_sessions(request)
    session = sessions.get(user["_id"])
    _guard_open_positions(session, testnet)
    if session is not None and session.testnet == testnet:
        await sessions.stop_user(user["_id"])
    users.delete_keys(user["_id"], testnet)
    logger.info(f"API keys deleted for user {user['_id']} ({'testnet' if testnet else 'live'})")
    return _me(request, users.get(user["_id"]))
