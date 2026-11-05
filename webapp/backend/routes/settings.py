"""
/api/settings — глобальний тумблер торгівлі + стратегії з їхніми параметрами.
Тонка HTTP-обгортка над:
  - core/state/settings_manager.py      -> trading.enabled (kill-switch)
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

from core.database.mongo_config import read_mode_override, save_mode_override
from core.strategies.registry import STRATEGY_REGISTRY
from core.state.settings_manager import MAX_STOP_ROI_LIMIT

from ..deps import (
    require_db,
    require_restart_event,
    require_settings_manager,
    require_strategy_settings,
    require_telegram_user,
    require_trader,
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


def _serialize_strategy(entry: Dict[str, Any], store) -> Dict[str, Any]:
    name = entry["strategy_name"]
    return {
        "name": name,
        "enabled": entry["enabled"],
        "modified": store.is_modified(name),
        "updated_at": entry["updated_at"],
        "params": [_serialize_param(k, v) for k, v in entry["params"].items()],
    }


def _strategy_response(store, name: str) -> Dict[str, Any]:
    entries = {e["strategy_name"]: e for e in store.list_strategies()}
    return _serialize_strategy(entries[name], store)


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
def get_settings(request: Request):
    settings_manager = require_settings_manager(request)
    store = require_strategy_settings(request)
    # Режим: testnet — поточний активний (з яким піднятий бот); mode_override —
    # що записано в Mongo (None => працюємо на дефолті з config.yaml).
    db = require_db(request)
    try:
        mode_override = read_mode_override(db.client)
    except Exception:
        mode_override = None
    return {
        "trading_enabled": settings_manager.get_trading_enabled(),
        "min_margin_usdt": settings_manager.get_min_margin_usdt(),
        "max_stop_roi_percent": settings_manager.get_max_stop_roi_percent(),
        "testnet": bool(getattr(request.app.state, "testnet", True)),
        "mode_override": mode_override,
        "strategies": [_serialize_strategy(entry, store) for entry in store.list_strategies()],
    }


@router.post("/mode")
async def set_mode(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    """Перемикання testnet <-> live з Mini App. Пише режим у mode-незалежну
    базу (control.mode) і сигналить супервізору main.py на штатний перезапуск
    у тому ж процесі (без редеплою). Guard: не перемикати з відкритими
    позиціями, якщо немає явного force=true (позиції лишилися б у старій базі
    без супроводу SL/TP до повернення в той режим)."""
    testnet = payload.get("testnet")
    if not isinstance(testnet, bool):
        raise HTTPException(status_code=422, detail="'testnet' must be a boolean")
    force = bool(payload.get("force", False))

    current = bool(getattr(request.app.state, "testnet", True))
    if testnet == current:
        return {"testnet": current, "restarting": False, "detail": "already in this mode"}

    trader = require_trader(request)
    open_count = len(getattr(trader, "open_positions", {}) or {})
    if open_count > 0 and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Є відкриті позиції — зміна режиму заблокована",
                "open_positions": open_count,
            },
        )

    restart_event = require_restart_event(request)
    db = require_db(request)
    save_mode_override(db.client, testnet)
    restart_event.set()

    logger.info(
        f"Mode switch requested via mini app: {'testnet' if testnet else 'live'} "
        f"(force={force}, open_positions={open_count}, user={user})"
    )
    return {"testnet": testnet, "restarting": True}


@router.post("/trading")
async def set_trading_enabled(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    settings_manager = require_settings_manager(request)
    enabled = _require_bool(payload)
    await settings_manager.set_trading_enabled(enabled)
    logger.info(f"Trading globally {'enabled' if enabled else 'disabled'} via mini app (user={user})")
    return {"trading_enabled": enabled}


@router.post("/risk")
async def set_risk_settings(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
):
    """Глобальні ризик-налаштування угод (застосовуються до наступних входів
    без рестарту): min_margin_usdt — мінімальна маржа (0 = вимкнено);
    max_stop_roi_percent — найбільший збиток на стопі в % ROI (1..90)."""
    settings_manager = require_settings_manager(request)

    def _number(key):
        value = payload.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"'{key}' must be a number")
        return float(value)

    if "min_margin_usdt" not in payload and "max_stop_roi_percent" not in payload:
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
    logger.info(f"Risk settings updated via mini app: {payload} (user={user})")
    return {
        "min_margin_usdt": settings_manager.get_min_margin_usdt(),
        "max_stop_roi_percent": settings_manager.get_max_stop_roi_percent(),
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
    user: dict = Depends(require_telegram_user),
):
    store = require_strategy_settings(request)
    _require_known_strategy(store, name)
    enabled = _require_bool(payload)

    strategy_manager = request.app.state.strategy_manager
    if strategy_manager is not None:
        strategy_manager.set_enabled(name, enabled)  # оновлює і БД, і live-інстанс
    else:
        store.set_enabled(name, enabled)

    logger.info(f"Strategy '{name}' {'enabled' if enabled else 'disabled'} via mini app (user={user})")
    return _strategy_response(store, name)


@router.post("/strategies/{name}/params")
async def update_strategy_param(
    name: str,
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: dict = Depends(require_telegram_user),
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

    logger.info(f"Strategy '{name}' param '{key}' updated via mini app (user={user})")
    return _strategy_response(store, name)


@router.post("/strategies/{name}/reset")
async def reset_strategy_params(
    name: str,
    request: Request,
    user: dict = Depends(require_telegram_user),
):
    store = require_strategy_settings(request)
    _require_known_strategy(store, name)

    _apply_params_live(request, name, store.reset_to_default(name))

    logger.info(f"Strategy '{name}' reset to defaults via mini app (user={user})")
    return _strategy_response(store, name)
