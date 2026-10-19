"""
Похідні метрики trade_analytics — чисті функції без БД (рахуються один раз
при відкритті й один раз при закритті угоди). Усі "pct" — у відсотках;
slippage > 0 означає ПОГІРШЕННЯ.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from ..common.position_math import parse_leverage, side_sign


def entry_derived_fields(
    side: Optional[str],
    entry_price: Optional[float],
    quantity: Optional[float],
    leverage: Any,
    margin_usdt: Optional[float],
    stop_loss_price_initial: Optional[float],
    reference_price: Optional[float],
    equity_at_entry: Optional[float],
    opened_at: datetime,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "opened_hour_utc": opened_at.hour,
        "opened_weekday": opened_at.weekday(),  # 0 = понеділок
    }
    leverage_value = parse_leverage(leverage)

    if entry_price and quantity:
        out["notional_usdt"] = entry_price * quantity

        if stop_loss_price_initial:
            distance = abs(entry_price - stop_loss_price_initial)
            out["stop_distance_pct"] = distance / entry_price * 100.0
            out["planned_risk_usdt"] = quantity * distance
            if leverage_value:
                out["stop_distance_roi_pct"] = out["stop_distance_pct"] * leverage_value

    if entry_price and reference_price and side in ("LONG", "SHORT"):
        out["entry_slippage_pct"] = side_sign(side) * (entry_price - reference_price) / reference_price * 100.0

    if equity_at_entry and equity_at_entry > 0:
        out["equity_at_entry"] = equity_at_entry
        if margin_usdt:
            out["margin_pct_of_equity"] = margin_usdt / equity_at_entry * 100.0
        if "planned_risk_usdt" in out:
            out["planned_risk_pct_of_equity"] = out["planned_risk_usdt"] / equity_at_entry * 100.0
    return out


def exit_derived_fields(
    row: Optional[dict],
    close_price: Optional[float],
    net_pnl: Optional[float],
    commission_open: Optional[float],
    commission_close: Optional[float],
    close_reason: Optional[str],
) -> Dict[str, Any]:
    """Метрики виходу з уже збереженого рядка trade_analytics."""
    out: Dict[str, Any] = {}
    if not row:
        return out

    entry_price = row.get("entry_price")
    side = row.get("side")
    sign = side_sign(side)
    moves = row.get("sl_moves") or []

    out["sl_moves_count"] = len(moves)
    final_stop = moves[-1].get("new_stop_price") if moves else row.get("stop_loss_price_initial")
    out["final_stop_price"] = final_stop

    planned_risk = row.get("planned_risk_usdt")
    if planned_risk and planned_risk > 0 and net_pnl is not None:
        out["r_multiple"] = net_pnl / planned_risk  # -1.0 = збиток рівно в запланований ризик

    if entry_price and close_price and side in ("LONG", "SHORT"):
        out["price_move_pct"] = sign * (close_price - entry_price) / entry_price * 100.0
        if close_reason == "stop_loss" and final_stop:
            # > 0: закрито ГІРШЕ за рівень стопу (проскальзування), у % від ціни входу
            out["stop_slippage_pct"] = sign * (final_stop - close_price) / entry_price * 100.0

    notional = row.get("notional_usdt")
    if notional and (commission_open is not None or commission_close is not None):
        fees = abs((commission_open or 0.0) + (commission_close or 0.0))
        out["commission_pct_of_notional"] = fees / notional * 100.0
    return out
