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

from ..deps import require_settings_manager, require_strategy_settings, require_telegram_user

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
    return {
        "trading_enabled": settings_manager.get_trading_enabled(),
        "strategies": [_serialize_strategy(entry, store) for entry in store.list_strategies()],
    }


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
