"""
Розміщення захисних ордерів (SL/TP) позиції — спільне для SimpleTrader
(перший SL/TP при відкритті) і TrailingStopManager (перенесення SL).
"""

import time
from typing import Optional

from ..common.market_data import extract_order_id
from ..common.position_math import close_order_side


def new_client_order_id(prefix: str) -> str:
    """clientOrderID, за яким бот потім впізнає власні SL/TP в ORDER_TRADE_UPDATE."""
    return f"{prefix}-{int(time.time() * 1000)}"


async def place_stop_market(
    exchange,
    *,
    symbol: str,
    side: str,
    quantity: float,
    stop_price: float,
    client_order_id: str,
    working_type: Optional[str] = None,
) -> Optional[str]:
    """STOP_MARKET з closePosition=true: SL завжди закриває позицію цілком, тож
    біржа сама квадратить фактичний залишок (без хвостів і помилок округлення
    110424). quantity BingX все одно вимагає (див. BingXClient.create_order).
    Повертає orderId або None, якщо біржа його не віддала; помилки біржі
    (BingXAPIError) прокидаються викликачу."""
    response = await exchange.create_order(
        symbol=symbol,
        side=close_order_side(side),
        order_type='STOP_MARKET',
        quantity=quantity,
        stop_price=stop_price,
        position_side=side,
        close_position=True,
        client_order_id=client_order_id,
        working_type=working_type,
    )
    order_id = extract_order_id(response)
    return str(order_id) if order_id else None


async def place_take_profit_market(
    exchange,
    *,
    symbol: str,
    side: str,
    quantity: float,
    stop_price: float,
    client_order_id: str,
    close_position: bool = False,
) -> Optional[str]:
    """TAKE_PROFIT_MARKET. close_position=True — для останнього рівня TP."""
    response = await exchange.create_order(
        symbol=symbol,
        side=close_order_side(side),
        order_type='TAKE_PROFIT_MARKET',
        quantity=quantity,
        stop_price=stop_price,
        position_side=side,
        close_position=close_position,
        client_order_id=client_order_id,
    )
    order_id = extract_order_id(response)
    return str(order_id) if order_id else None
