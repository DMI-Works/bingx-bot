"""
Перший SL і TP-ордери позиції одразу після відкриття.
(Подальше перенесення SL — у TrailingStopManager.)
"""

import logging
from typing import List, Optional, Tuple

from ..events import EventBus, publish_error
from ..exchange.errors import BingXAPIError
from ..exchange.orders import new_client_order_id, place_stop_market, place_take_profit_market

logger = logging.getLogger(__name__)

_SL_MAX_ATTEMPTS = 3
# запас понад відстань стопу при переанкорингу до mark price: навіть mark
# price, отримана через REST, встигає "постаріти" за round-trip на волатильних алтах
_SL_REANCHOR_SAFETY = 0.0005


class ProtectiveOrderPlacer:
    def __init__(self, exchange, event_bus: EventBus):
        self.exchange = exchange
        self.event_bus = event_bus

    async def _notify(self, context: str, error: str, critical: bool = False) -> None:
        await publish_error(self.event_bus, context=context, error=error, critical=critical, source="SimpleTrader")

    async def create_stop_loss(
        self,
        symbol: str,
        side: str,
        quantity: float,
        stop_loss_price: float,
        entry_price: Optional[float] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Повертає (order_id, client_order_id). Якщо біржа відхилила стоп як
        "застарілий" (ціна вже пройшла рівень) — переанкорює його до mark
        price з тією ж відстанню від ціни і пробує ще раз.
        """
        unprotected_context = f"Не вдалося поставити SL для {symbol} — позиція без захисту!"
        current_stop_price = stop_loss_price

        buffer_percent = None
        if entry_price and entry_price > 0 and stop_loss_price:
            buffer_percent = abs(entry_price - stop_loss_price) / entry_price

        client_order_id = new_client_order_id("sl")
        last_error: Optional[BingXAPIError] = None

        for attempt in range(1, _SL_MAX_ATTEMPTS + 1):
            try:
                order_id = await place_stop_market(
                    self.exchange,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    stop_price=current_stop_price,
                    client_order_id=client_order_id,
                )
                logger.info(
                    f"Stop loss created: {symbol} @ {current_stop_price}, "
                    f"orderId={order_id}, clientOrderId={client_order_id}, closePosition=true"
                )
                return order_id, client_order_id

            except BingXAPIError as e:
                last_error = e
                is_stale = 'current price' in (e.msg or '').lower()
                if not (is_stale and buffer_percent is not None and attempt < _SL_MAX_ATTEMPTS):
                    break
                try:
                    mark_price = await self.exchange.get_mark_price(symbol)
                except Exception as fetch_err:
                    logger.error(f"Failed to fetch mark price to re-anchor SL for {symbol}: {fetch_err}")
                    break
                safety_percent = buffer_percent + _SL_REANCHOR_SAFETY
                new_stop_price = (
                    mark_price * (1 - safety_percent) if side == 'LONG'
                    else mark_price * (1 + safety_percent)
                )
                logger.warning(
                    f"SL for {symbol} rejected as stale (price already passed target "
                    f"{current_stop_price}). Re-anchoring to mark price {mark_price} -> "
                    f"new stop={new_stop_price}, retrying (attempt {attempt + 1})"
                )
                current_stop_price = new_stop_price

            except Exception as e:
                logger.error(f"Failed to create stop loss: {e}")
                await self._notify(unprotected_context, str(e), critical=True)
                return None, None

        logger.error(
            f"Failed to create stop loss for {symbol} after {_SL_MAX_ATTEMPTS} attempts "
            f"(last tried stopPrice={current_stop_price}): "
            f"{last_error.code if last_error else '?'} {last_error.msg if last_error else ''}"
        )
        await self._notify(
            unprotected_context,
            f"{last_error.code} {last_error.msg}" if last_error else "unknown error",
            critical=True,
        )
        return None, None

    async def create_take_profit_orders(
        self, symbol: str, side: str, quantity: float, tp_levels: list
    ) -> List[dict]:
        """
        Повертає [{'order_id': ..., 'client_order_id': ...}] по кожному рівню TP.

        Останній рівень завжди closePosition=true — квадратить фактичний
        залишок позиції, прибираючи хвости від округлення quantity на
        попередніх часткових рівнях. Проміжні рівні закриваються точним
        quantity (closePosition на них закрив би позицію цілком).
        """
        results = []
        last_index = len(tp_levels) - 1

        logger.info(f"Creating {len(tp_levels)} take profit orders for {symbol} {side}")
        for i, tp_level in enumerate(tp_levels):
            client_order_id = new_client_order_id(f"tp{i + 1}")
            is_last_level = i == last_index

            try:
                tp_price = tp_level['price']
                order_id = await place_take_profit_market(
                    self.exchange,
                    symbol=symbol,
                    side=side,
                    quantity=quantity if is_last_level else quantity * (tp_level['close_percent'] / 100),
                    stop_price=tp_price,
                    client_order_id=client_order_id,
                    close_position=is_last_level,
                )
                results.append({'order_id': order_id, 'client_order_id': client_order_id})
                logger.info(
                    f"Take profit {i + 1}{'(final, closePosition=true)' if is_last_level else ''} created: "
                    f"{symbol} @ {tp_price}, orderId={order_id}, clientOrderId={client_order_id}"
                )

            except BingXAPIError as e:
                logger.error(f"Failed to create take profit {i + 1} for {symbol}: {e.code} {e.msg}")
                await self._notify(
                    f"Не вдалося поставити TP{i + 1} для {symbol} (SL, якщо є, залишається активним)",
                    f"{e.code} {e.msg}",
                )
                results.append({'order_id': None, 'client_order_id': None})
            except Exception as e:
                logger.error(f"Failed to create take profit {i + 1}: {e}")
                results.append({'order_id': None, 'client_order_id': None})

        return results
