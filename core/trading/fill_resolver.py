"""
Визначення фактичної ціни входу й виконаного обсягу MARKET-ордера.

Синхронна відповідь create_order часто повертає avgPrice=0 / executedQty=0 —
фактичне виконання ще не долетіло до REST-шару біржі. Тому джерела
перебираються по черзі:
  1) сама відповідь create_order (часто вже FILLED з реальними цифрами);
  2) опитування GET /trade/order, поки не з'явиться FILLED;
  3) фактичний стан позиції на біржі (entryPrice/positionAmt).
"""

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from ..common.market_data import to_float

logger = logging.getLogger(__name__)


def parse_fill(order_info: Optional[Dict[str, Any]]) -> Tuple[Optional[str], float, float]:
    """Безпечно витягує (status, avg_price, executed_qty) з об'єкта ордера біржі."""
    if not order_info:
        return None, 0.0, 0.0
    return (
        order_info.get('status'),
        to_float(order_info.get('avgPrice', 0)),
        to_float(order_info.get('executedQty', 0)),
    )


def _is_confirmed(order_info: Optional[Dict[str, Any]]) -> bool:
    status, avg_price, executed_qty = parse_fill(order_info)
    return status == 'FILLED' and avg_price > 0 and executed_qty > 0


async def wait_for_confirmed_fill(
    exchange,
    symbol: str,
    order_id: str,
    max_attempts: int = 6,
    delay_seconds: float = 0.3,
) -> Optional[Dict[str, Any]]:
    """Опитує GET /trade/order, поки не отримає FILLED з реальними
    avgPrice/executedQty. Не дочекались — повертає останнє, що бачили."""
    last_order_info = None

    for attempt in range(1, max_attempts + 1):
        try:
            order_info = await exchange.get_order(symbol, order_id)
        except Exception as e:
            logger.warning(f"get_order attempt {attempt} failed for {symbol} order {order_id}: {e}")
            order_info = None

        if order_info:
            last_order_info = order_info
            if attempt == 1:
                logger.debug(f"Raw get_order response for {symbol} order {order_id}: {order_info}")
            if _is_confirmed(order_info):
                _, avg_price, executed_qty = parse_fill(order_info)
                logger.info(
                    f"Confirmed fill for {symbol} order {order_id} on attempt {attempt}: "
                    f"avgPrice={avg_price}, executedQty={executed_qty}"
                )
                return order_info

        if attempt < max_attempts:
            await asyncio.sleep(delay_seconds)

    logger.warning(
        f"Could not confirm fill for {symbol} order {order_id} after {max_attempts} attempts "
        f"(last seen: {last_order_info}). Falling back to whatever data is available."
    )
    return last_order_info


async def fetch_entry_from_positions(exchange, symbol: str, side: str) -> Optional[Dict[str, float]]:
    """Fallback: реальна ціна входу з фактичного стану позиції на біржі —
    позиція вже може бути відкрита, навіть якщо статус ордера ще не FILLED."""
    try:
        positions = await exchange.get_positions()
    except Exception as e:
        logger.warning(f"Failed to fetch positions as entry_price fallback for {symbol}: {e}")
        return None

    for pos in positions:
        if pos.get('symbol') != symbol or pos.get('positionSide') != side:
            continue
        entry_price = to_float(pos.get('entryPrice', 0))
        position_amt = abs(to_float(pos.get('positionAmt', 0)))
        if entry_price > 0 and position_amt > 0:
            logger.info(
                f"Fallback entry_price for {symbol} {side} from positions endpoint: "
                f"entryPrice={entry_price}, positionAmt={position_amt}"
            )
            return {'entry_price': entry_price, 'quantity': position_amt}

    return None


async def resolve_entry_fill(
    exchange,
    symbol: str,
    side: str,
    order_id: str,
    create_response: Dict[str, Any],
    requested_quantity: float,
) -> Tuple[float, float]:
    """(entry_price, executed_qty) MARKET-ордера. entry_price=0.0 — якщо жодне
    джерело не дало ціни (SL/TP тоді рахуються від неперевіреної ціни)."""
    data = create_response.get('data') if isinstance(create_response, dict) else None
    immediate_order_info = data.get('order') if isinstance(data, dict) else None

    if _is_confirmed(immediate_order_info):
        _, entry_price, executed_qty = parse_fill(immediate_order_info)
        logger.info(
            f"Fill confirmed directly from create_order response for {symbol} {side}: "
            f"avgPrice={entry_price}, executedQty={executed_qty} (no extra polling needed)"
        )
        return entry_price, executed_qty

    entry_price, executed_qty = 0.0, requested_quantity
    confirmed_order = await wait_for_confirmed_fill(exchange, symbol, order_id)
    if confirmed_order:
        _, confirmed_avg_price, confirmed_qty = parse_fill(confirmed_order)
        if confirmed_avg_price > 0:
            entry_price = confirmed_avg_price
        if confirmed_qty > 0:
            executed_qty = confirmed_qty

    if entry_price <= 0:
        fallback = await fetch_entry_from_positions(exchange, symbol, side)
        if fallback:
            return fallback['entry_price'], fallback['quantity']
        logger.warning(
            f"Failed to confirm entry_price for {symbol} {side} order {order_id} "
            f"(both order status and positions endpoint gave nothing usable) — "
            f"SL/TP will be based on unconfirmed price, may be inaccurate"
        )

    return entry_price, executed_qty
