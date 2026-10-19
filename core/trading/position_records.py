"""
Структура запису позиції в SimpleTrader.open_positions (і в positions.metadata).

Нові поля позиції додавай у new_bot_position() І у position_from_db_row()
з fallback-значенням — інакше позиції, відкриті до зміни, після рестарту
бота відновляться без цього поля.
"""

from typing import Optional

from ..common.position_metadata import load_metadata


def _empty_accounting() -> dict:
    return {
        'realized_pnl_accum': 0.0,
        # Комісії розділено на open/close (ТЗ, задача 2): комісію ВІДКРИТТЯ
        # REST create_order не повертає — вона приходить лише через WS
        # ORDER_TRADE_UPDATE по order_id входу (див. SimpleTrader._handle_order_update).
        'commission_open_accum': 0.0,
        'commission_close_accum': 0.0,
        # Funding по кожній позиції поки не відстежується — поле бере участь
        # у формулі net_pnl наперед, щоб її не довелось міняти.
        'funding_fee_accum': 0.0,
    }


def new_bot_position(
    *,
    order_id: str,
    symbol: str,
    side: str,
    quantity: float,
    entry_price: float,
    leverage,
    stop_loss_price: Optional[float],
    take_profit_levels: Optional[list],
    strategy: Optional[str],
    trail_meta: Optional[dict],
) -> dict:
    return {
        'order_id': order_id,
        'symbol': symbol,
        'side': side,
        'quantity': quantity,
        'entry_price': entry_price,
        'leverage': leverage,
        'stop_loss_price': stop_loss_price,
        # НЕЗМІННА копія першого SL. TrailingStopManager використовує його лише
        # як fallback, якщо не вдалось перевиставити стоп на черговому рівні.
        'initial_stop_loss_price': stop_loss_price,
        'take_profit_levels': take_profit_levels,
        'opened_by': 'bot',
        'sl_order_id': None,
        'tp_order_ids': [],
        'sl_client_order_id': None,
        'tp_client_order_ids': [],
        'strategy': strategy,
        # параметри супроводу стопу від стратегії (напр. atr_3step)
        'trail_meta': trail_meta,
        **_empty_accounting(),
        'remaining_quantity': quantity,
    }


def new_manual_position(*, order_id: str, symbol: str, side: str, entry_price: float, quantity: float) -> dict:
    """Позиція, відкрита вручну на біржі. Entry-ордер бот не бачив, тож
    commission_open тут назавжди 0.0 — це не баг."""
    return {
        'order_id': order_id,
        'symbol': symbol,
        'side': side,
        'entry_price': entry_price,
        'quantity': quantity,
        'opened_by': 'user',
        'sl_order_id': None,
        'tp_order_ids': [],
        'sl_client_order_id': None,
        'tp_client_order_ids': [],
        'strategy': None,
        **_empty_accounting(),
    }


def position_from_db_row(row: dict) -> dict:
    """Відновлює запис позиції з рядка positions (після рестарту бота)."""
    metadata = load_metadata(row['metadata'])
    return {
        'order_id': row['order_id'],
        'symbol': row['symbol'],
        'side': row['side'],
        'quantity': metadata.get('quantity', 0),
        'entry_price': metadata.get('entry_price', 0.0),
        'leverage': metadata.get('leverage', 10),
        'stop_loss_price': metadata.get('stop_loss_price'),
        # позиції, відкриті ДО появи initial_stop_loss_price — fallback на поточний SL
        'initial_stop_loss_price': metadata.get('initial_stop_loss_price', metadata.get('stop_loss_price')),
        'take_profit_levels': metadata.get('take_profit_levels'),
        'opened_by': metadata.get('opened_by', 'bot'),
        'sl_order_id': metadata.get('sl_order_id'),
        'tp_order_ids': metadata.get('tp_order_ids', []),
        'sl_client_order_id': metadata.get('sl_client_order_id'),
        'tp_client_order_ids': metadata.get('tp_client_order_ids', []),
        'strategy': metadata.get('strategy'),
        'trail_meta': metadata.get('trail_meta'),
        'realized_pnl_accum': metadata.get('realized_pnl_accum', 0.0),
        # Старе єдине поле commission_accum — це накопичена комісія ЗАКРИТТЯ
        # (лише її рахували до розділення на open/close), тому мапиться в close.
        # Комісію відкриття таких позицій уже не відновити — open лишається 0.0.
        'commission_open_accum': metadata.get('commission_open_accum', 0.0),
        'commission_close_accum': metadata.get(
            'commission_close_accum', metadata.get('commission_accum', 0.0)
        ),
        'funding_fee_accum': metadata.get('funding_fee_accum', 0.0),
        'remaining_quantity': metadata.get('remaining_quantity') or metadata.get('quantity', 0),
        'closing_trade_ids': metadata.get('closing_trade_ids', []),
        'closing_orders': metadata.get('closing_orders', []),
    }
