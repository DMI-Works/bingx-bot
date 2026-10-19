"""
Розбір сирих даних BingX (WS-тики, REST-відповіді), який раніше
повторювався в кожному споживачі: стратегіях, трейлінгу, трекерах,
RiskManager, SymbolSelector, webapp API і Telegram-боті.
"""

from typing import Any, Iterable, List, Optional, Set, Tuple


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def parse_price_tick(data: Any) -> Optional[Tuple[str, float]]:
    """(symbol, price) з payload події PRICE_UPDATED (@trade) або None, якщо
    тік порожній чи зіпсований. BingX шле або список угод, або одну угоду —
    беремо першу."""
    if not data:
        return None
    tick = data[0] if isinstance(data, list) else data
    if not isinstance(tick, dict):
        return None
    symbol = tick.get('s')
    try:
        price = float(tick.get('p', 0))
    except (TypeError, ValueError):
        return None
    if not symbol or price <= 0:
        return None
    return symbol, price


def position_amount(position: dict) -> float:
    """positionAmt позиції з /user/positions (0.0, якщо поле зіпсоване)."""
    return to_float(position.get('positionAmt', 0))


def filter_open_positions(positions: Optional[Iterable[dict]]) -> List[dict]:
    """Лише реально відкриті позиції біржі (positionAmt != 0)."""
    return [p for p in positions or [] if position_amount(p) != 0]


def held_symbols(positions: Optional[Iterable[dict]]) -> Set[str]:
    """Символи, по яких на біржі є відкрита позиція."""
    return {p.get('symbol') for p in filter_open_positions(positions)}


def extract_order_id(response: Any) -> Optional[Any]:
    """orderId з відповіді POST /trade/order. BingX кладе його в
    data.order.orderId, але трапляються і data.orderId / orderId."""
    if not isinstance(response, dict):
        return None
    data = response.get('data')
    if isinstance(data, dict):
        order = data.get('order')
        if isinstance(order, dict) and order.get('orderId'):
            return order['orderId']
        if data.get('orderId'):
            return data['orderId']
    return response.get('orderId')


def extract_account_balance(response: Any) -> Optional[dict]:
    """balance-dict з відповіді /user/balance або None, якщо біржа відповіла
    помилкою чи неочікуваним форматом."""
    if not isinstance(response, dict) or response.get('code') != 0 or 'data' not in response:
        return None
    return response['data'].get('balance', {}) or {}
