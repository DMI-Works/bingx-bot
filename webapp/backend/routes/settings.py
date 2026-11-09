"""
/api/settings — глобальний тумблер торгівлі + стратегії з їхніми параметрами.
Тонка HTTP-обгортка над:
  - core/state/settings_manager.py      -> особисті налаштування користувача
  - core/database/strategy_settings.py  -> StrategySettingsStore (БД)
  - core/strategies/strategies_setup.py -> StrategyManager (live-інстанси)
Мутуючі ендпоінти вимагають Telegram-авторизації: вони керують реальною торгівлею.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from core.strategies.param_catalog import (
    catalog_lookup,
    coerce_param_value,
    infer_item_kind,
    infer_param_kind,
)

from core.engine.user_session import DISABLED_STRATEGIES_KEY
from core.state.settings_manager import MAX_OPEN_POSITIONS_LIMIT, MAX_STOP_ROI_LIMIT
from core.strategies.registry import STRATEGY_REGISTRY

from ..deps import (
    require_admin,
    require_session,
    require_sessions,
    require_strategy_settings,
    require_user,
    require_users,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings")


def _serialize_param(key: str, value: Any) -> Dict[str, Any]:
    label, description = catalog_lookup(key)
    kind = infer_param_kind(value) or "text"
    item = {"key": key, "label": label, "description": description, "kind": kind, "value": value}
    if kind == "list":
        item["item_kind"] = infer_item_kind(value)
    return item


def _is_admin(session) -> bool:
    return session.user.get("role") == "admin"


def _serialize_strategy(entry: Dict[str, Any], store, session) -> Dict[str, Any]:
    """enabled — особистий вибір користувача, global_enabled — чи стратегія
    взагалі працює (адмін). Параметри спільні й бачить їх лише адмін."""
    name = entry["strategy_name"]
    admin = _is_admin(session)
    return {
        "name": name,
        "enabled": name not in session.disabled_strategies(),
        "global_enabled": entry["enabled"],
        "can_edit": admin,
        "modified": store.is_modified(name) if admin else False,
        "updated_at": entry["updated_at"],
        "params": [_serialize_param(k, v) for k, v in entry["params"].items()] if admin else [],
    }


def _strategy_response(store, name: str, session) -> Dict[str, Any]:
    entries = {e["strategy_name"]: e for e in store.list_strategies()}
    return _serialize_strategy(entries[name], store, session)


def _require_known_strategy(store, name: str) -> Dict[str, Any]:
    params = store.get_params(name)
    if params is None:
        raise HTTPException(status_code=404, detail=f"Unknown strategy '{name}'")
    return params


def _require_bool(payload: Dict[str, Any], key: str = "enabled") -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise HTTPException(status_code=422, detail=f"'{key}' must be a boolean")
    return value


def _apply_params_live(request: Request, name: str, params: Dict[str, Any]) -> None:
    """Є live StrategyManager — нові параметри застосовуються до працюючої
    стратегії одразу, без рестарту. Немає (локальний запуск лише веб-частини) —
    зміни вже в БД і підхопляться при наступному старті."""
    strategy_manager = request.app.state.strategy_manager
    if strategy_manager is not None:
        strategy_manager.apply_params(name, params)


@router.get("")
def get_settings(request: Request, session=Depends(require_session)):
    settings_manager = session.settings_manager
    store = require_strategy_settings(request)
    return {
        "trading_enabled": settings_manager.get_trading_enabled(),
        "min_margin_usdt": settings_manager.get_min_margin_usdt(),
        "max_stop_roi_percent": settings_manager.get_max_stop_roi_percent(),
        "max_open_positions": session.risk_manager.effective_max_open_positions(),
        "testnet": session.testnet,
        "is_admin": _is_admin(session),
        "strategies": [_serialize_strategy(entry, store, session) for entry in store.list_strategies()],
    }


@router.post("/mode")
async def set_mode(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_user),
):
    """Перемикання testnet <-> live СВОГО рахунку: перезапускає лише сесію цього
    користувача. Guard: з відкритими позиціями — лише з force=true (вони лишились
    би без супроводу SL/TP до повернення в цей режим)."""
    testnet = payload.get("testnet")
    if not isinstance(testnet, bool):
        raise HTTPException(status_code=422, detail="'testnet' must be a boolean")
    force = bool(payload.get("force", False))

    users = require_users(request)
    sessions = require_sessions(request)
    user_id = user["_id"]
    current = bool(user.get("testnet", True))
    if testnet == current:
        return {"testnet": current, "restarting": False, "detail": "already in this mode"}
    if not users.has_keys(user, testnet):
        raise HTTPException(
            status_code=409,
            detail={"code": "no_keys", "message": f"Нет API-ключей для режима {'Testnet' if testnet else 'Live'}"},
        )

    session = sessions.get(user_id)
    open_count = len(session.trader.open_positions) if session is not None else 0
    if open_count > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "open_positions",
                "message": "Есть открытые позиции — смена режима заблокирована",
                "open_positions": open_count,
            },
        )

    users.set_testnet(user_id, testnet)
    if await sessions.start_user(user_id) is None:
        users.set_testnet(user_id, current)
        await sessions.start_user(user_id)
        raise HTTPException(status_code=502, detail="Не удалось запустить торговлю в этом режиме")

    logger.info(f"Mode switched to {'testnet' if testnet else 'live'} (force={force}, user={user_id})")
    return {"testnet": testnet, "restarting": True}


@router.post("/trading")
async def set_trading_enabled(payload: Dict[str, Any] = Body(...), session=Depends(require_session)):
    enabled = _require_bool(payload)
    await session.settings_manager.set_trading_enabled(enabled)
    logger.info(f"Trading {'enabled' if enabled else 'disabled'} via mini app (user={session.user_id})")
    return {"trading_enabled": enabled}


@router.post("/risk")
async def set_risk_settings(payload: Dict[str, Any] = Body(...), session=Depends(require_session)):
    """Глобальні ризик-налаштування угод (застосовуються до наступних входів
    без рестарту): min_margin_usdt — мінімальна маржа (0 = вимкнено);
    max_stop_roi_percent — найбільший збиток на стопі в % ROI (1..90);
    max_open_positions — скільки позицій може бути відкрито одночасно."""
    settings_manager = session.settings_manager

    def _number(key):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"'{key}' must be a number")
        return float(value)

    if not {"min_margin_usdt", "max_stop_roi_percent", "max_open_positions"} & payload.keys():
        raise HTTPException(status_code=422, detail="nothing to update")
    if "min_margin_usdt" in payload:
        value = _number("min_margin_usdt")
        if value < 0:
            raise HTTPException(status_code=422, detail="'min_margin_usdt' must be >= 0")
        await settings_manager.set_min_margin_usdt(value)
    if "max_stop_roi_percent" in payload:
        value = _number("max_stop_roi_percent")
        if not 1 <= value <= MAX_STOP_ROI_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=f"'max_stop_roi_percent' must be between 1 and {MAX_STOP_ROI_LIMIT:g} (ликвидация ~100%)",
            )
        await settings_manager.set_max_stop_roi_percent(value)
    if "max_open_positions" in payload:
        value = _number("max_open_positions")
        if int(value) != value or not 1 <= value <= MAX_OPEN_POSITIONS_LIMIT:
            raise HTTPException(status_code=422, detail=f"Введите целое число от 1 до {MAX_OPEN_POSITIONS_LIMIT}")
        await settings_manager.set_max_open_positions(int(value))
    logger.info(f"Risk settings updated via mini app: {payload} (user={session.user_id})")
    return {
        "min_margin_usdt": settings_manager.get_min_margin_usdt(),
        "max_stop_roi_percent": settings_manager.get_max_stop_roi_percent(),
        "max_open_positions": session.risk_manager.effective_max_open_positions(),
    }


def _validate_strategy_params(name: str, params: Dict[str, Any]) -> None:
    """Стратегії з _parse_params мовчки відкидають некоректні параметри
    (лишаються на старих) — без цієї перевірки мініапп показував би значення,
    з яким бот насправді не торгує (напр. risk_per_trade_percent=50 > 10)."""
    cls = STRATEGY_REGISTRY.get(name)
    parse = getattr(cls, "_parse_params", None)
    if parse is None:
        return
    try:
        parse(params)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Недопустимое значение: {e}")


@router.post("/strategies/{name}/enabled")
async def set_strategy_enabled(
    name: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    session=Depends(require_session),
):
    """Особистий вибір: приймати сигнали цієї стратегії на свій рахунок чи ні."""
    store = require_strategy_settings(request)
    _require_known_strategy(store, name)
    enabled = _require_bool(payload)
    disabled = session.disabled_strategies()
    disabled.discard(name) if enabled else disabled.add(name)
    await session.settings_manager.set(DISABLED_STRATEGIES_KEY, sorted(disabled))
    logger.info(f"Strategy '{name}' {'on' if enabled else 'off'} for user {session.user_id}")
    return _strategy_response(store, name, session)


@router.post("/strategies/{name}/global-enabled")
async def set_strategy_global_enabled(
    name: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    session=Depends(require_session),
    admin: dict = Depends(require_admin),
):
    """Вмикає/вимикає генерацію сигналів стратегії для всіх (адмін)."""
    store = require_strategy_settings(request)
    _require_known_strategy(store, name)
    enabled = _require_bool(payload)
    strategy_manager = request.app.state.strategy_manager
    if strategy_manager is not None:
        strategy_manager.set_enabled(name, enabled)
    else:
        store.set_enabled(name, enabled)
    logger.info(f"Strategy '{name}' globally {'enabled' if enabled else 'disabled'} (admin={admin['_id']})")
    return _strategy_response(store, name, session)


@router.post("/strategies/{name}/params")
async def update_strategy_param(
    name: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    session=Depends(require_session),
    admin: dict = Depends(require_admin),
):
    store = require_strategy_settings(request)
    current = _require_known_strategy(store, name)

    key = payload.get("key")
    if not isinstance(key, str) or key not in current:
        raise HTTPException(status_code=422, detail=f"Unknown param key for strategy '{name}'")
    if "value" not in payload:
        raise HTTPException(status_code=422, detail="'value' is required")

    try:
        current[key] = coerce_param_value(current[key], payload["value"])
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    _validate_strategy_params(name, current)
    store.update_params(name, current)
    _apply_params_live(request, name, current)

    logger.info(f"Strategy '{name}' param '{key}' updated via mini app (admin={admin['_id']})")
    return _strategy_response(store, name, session)


@router.post("/strategies/{name}/reset")
async def reset_strategy_params(
    name: str,
    request: Request,
    session=Depends(require_session),
    admin: dict = Depends(require_admin),
):
    store = require_strategy_settings(request)
    _require_known_strategy(store, name)

    _apply_params_live(request, name, store.reset_to_default(name))

    logger.info(f"Strategy '{name}' reset to defaults via mini app (admin={admin['_id']})")
    return _strategy_response(store, name, session)
