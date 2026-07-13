import asyncio
import logging
from typing import Optional
from datetime import datetime

from ..exchange import BingXClient
from ..execution import Order, OrderManager, OrderStatus, OrderSide, OrderType
from ..state import Position, PositionManager, PositionSide
from ..events import EventBus, Event, EventType
from ..database import Database


logger = logging.getLogger(__name__)


class ExecutionEngine:
    def __init__(
        self,
        exchange: BingXClient,
        order_manager: OrderManager,
        position_manager: PositionManager,
        event_bus: EventBus,
        db: Database,
        max_open_positions: int = 3
    ):
        self.exchange = exchange
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.event_bus = event_bus
        self.db = db
        self.max_open_positions = max_open_positions  

        self.max_order_retries = 3
        self.order_retry_delay = 2

        self.event_bus.subscribe(EventType.SIGNAL_GENERATED, self._handle_signal)
        self.event_bus.subscribe(EventType.ORDER_FILLED, self._handle_exchange_order_update)
        logger.info("ExecutionEngine initialized")

    async def _handle_signal(self, event: Event) -> None:
        signal = event.data
        action = signal.get('action')

        if action == 'OPEN':
            await self.open_position(
                symbol=signal['symbol'],
                side=signal['side'],
                quantity=signal['quantity'],
                leverage=signal.get('leverage', 10),
                stop_loss_price=signal.get('stop_loss_price'),
                take_profit_levels=signal.get('take_profit_levels')
            )

        elif action == 'CLOSE':
            await self.close_position(
                symbol=signal['symbol'],
                side=signal['side']
            )

    async def open_position(
        self,
        symbol: str,
        side: str,
        quantity: float,
        leverage: int = 10,
        stop_loss_price: Optional[float] = None,
        take_profit_levels: Optional[list] = None
    ) -> Optional[Position]:

        try:
            # Перевіряємо, чи вже є відкрита позиція з таким же символом і напрямком
            position_side = PositionSide.LONG if side == 'LONG' else PositionSide.SHORT
            existing_position = self.position_manager.get_position(symbol, position_side)

            if existing_position:
                logger.warning(f"Position already exists: {symbol} {side}, skipping")
                return existing_position

            current_count = self.position_manager.get_position_count()
            if current_count >= self.max_open_positions:
                logger.warning(
                    f"Max open positions limit reached ({current_count}/{self.max_open_positions}), "
                    f"skipping {symbol} {side}"
                )
                return None

            logger.info(f"Opening position: {symbol} {side} {quantity}")

            await self.exchange.set_leverage(symbol, leverage)

            order = Order(
                symbol=symbol,
                side=OrderSide.BUY if side == 'LONG' else OrderSide.SELL,
                order_type=OrderType.MARKET,
                quantity=quantity
            )
            order = await self.order_manager.create_order(order)

            exchange_order = await self._send_order_with_retry(order)

            if not exchange_order:
                logger.error(f"Failed to send order after retries")
                return None

            logger.info(f"Exchange order response: {exchange_order}")

            # Отримуємо orderId та статус з відповіді (BingX повертає у data.order)
            order_id = None
            order_status = None
            avg_price = None
            executed_qty = None

            if 'data' in exchange_order and 'order' in exchange_order['data']:
                order_data = exchange_order['data']['order']
                order_id = order_data.get('orderId')
                order_status = order_data.get('status')
                avg_price = float(order_data.get('avgPrice', 0))
                executed_qty = float(order_data.get('executedQty', 0))
            elif 'orderId' in exchange_order:
                order_id = exchange_order.get('orderId')
                order_status = exchange_order.get('status')
                avg_price = float(exchange_order.get('avgPrice', 0))
                executed_qty = float(exchange_order.get('executedQty', 0))

            # Якщо ордер вже заповнений (MARKET ордери часто заповнюються миттєво)
            # Перевіряємо статус FILLED або executed_qty близький до order.quantity (з урахуванням округлення біржі)
            if order_status == 'FILLED' and avg_price > 0:
                logger.info(f"Order filled immediately: {order.id}")
                await self.order_manager.update_order_status(
                    order,
                    OrderStatus.FILLED,
                    exchange_order_id=order_id,
                    filled_quantity=executed_qty if executed_qty > 0 else order.quantity,
                    average_price=avg_price
                )
            else:
                # Ордер ще не заповнений, встановлюємо статус SENT
                await self.order_manager.update_order_status(
                    order,
                    OrderStatus.SENT,
                    exchange_order_id=order_id
                )

                logger.info(f"Waiting for order fill: {order.id}, exchange_order_id: {order.exchange_order_id}")
                filled_order = await self._wait_for_order_fill(order)

                if not filled_order or not filled_order.is_filled():
                    logger.error(f"Order not filled: {order.id}, status: {filled_order.status if filled_order else 'None'}")
                    return None

            logger.info(f"Order filled: {order.id} @ {order.average_price}")

            entry_price = order.average_price
            actual_quantity = order.filled_quantity if order.filled_quantity else quantity
            margin = (entry_price * quantity) / leverage

            position = Position(
                symbol=symbol,
                side=PositionSide.LONG if side == 'LONG' else PositionSide.SHORT,
                entry_price=entry_price,
                quantity=actual_quantity,
                leverage=leverage,
                margin=margin,
                stop_loss_price=stop_loss_price,
                take_profit_levels=take_profit_levels
            )

            position = await self.position_manager.open_position(position)

            if stop_loss_price:
                await self._create_stop_loss_with_retry(position, stop_loss_price)

            if take_profit_levels:
                await self._create_take_profit_orders(position, take_profit_levels)

            logger.info(f"Position opened: {position.symbol} {position.side.value}")
            return position

        except Exception as e:
            logger.error(f"Failed to open position: {e}", exc_info=True)

            if self.event_bus:
                await self.event_bus.publish(Event(
                    type=EventType.ERROR,
                    data={'error': str(e), 'context': 'open_position'},
                    source="ExecutionEngine"
                ))

            return None

    async def close_position(self, symbol: str, side: str) -> bool:
        try:
            position_side = PositionSide.LONG if side == 'LONG' else PositionSide.SHORT
            position = self.position_manager.get_position(symbol, position_side)

            if not position:
                logger.warning(f"Position not found: {symbol} {side}")
                return False

            logger.info(f"Closing position: {symbol} {side}")

            close_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY

            order = Order(
                symbol=symbol,
                side=close_side,
                order_type=OrderType.MARKET,
                quantity=position.quantity
            )

            order = await self.order_manager.create_order(order)

            exchange_order = await self._send_order_with_retry(order, reduce_only=True, position_side=position.side.value)

            if not exchange_order:
                logger.error(f"Failed to send close order after retries")
                return False

            await self.order_manager.update_order_status(
                order,
                OrderStatus.SENT,
                exchange_order_id=exchange_order.get('orderId')
            )

            filled_order = await self._wait_for_order_fill(order)

            if not filled_order or not filled_order.is_filled():
                logger.error(f"Close order not filled: {order.id}")
                return False

            close_price = filled_order.average_price
            realized_pnl = position.calculate_pnl(close_price)

            await self.position_manager.close_position(position, close_price, realized_pnl)

            logger.info(f"Position closed: {symbol} {side} PnL: {realized_pnl:.2f}")
            return True

        except Exception as e:
            logger.error(f"Failed to close position: {e}", exc_info=True)

            if self.event_bus:
                await self.event_bus.publish(Event(
                    type=EventType.ERROR,
                    data={'error': str(e), 'context': 'close_position'},
                    source="ExecutionEngine"
                ))

            return False

    async def _send_order_with_retry(self, order: Order, reduce_only: bool = False,  position_side: Optional[str] = None) -> Optional[dict]:
        for attempt in range(self.max_order_retries):
            try:
                exchange_order = await self.exchange.create_order(
                    symbol=order.symbol,
                    side=order.side.value,
                    order_type=order.order_type.value,
                    quantity=order.quantity,
                    price=order.price,
                    stop_price=order.stop_price,
                    reduce_only=reduce_only,
                    position_side=position_side
                )

                return exchange_order

            except Exception as e:
                logger.warning(f"Order send attempt {attempt + 1} failed: {e}")

                if attempt < self.max_order_retries - 1:
                    await asyncio.sleep(self.order_retry_delay)
                else:
                    logger.error(f"Order send failed after {self.max_order_retries} attempts")
                    await self.order_manager.update_order_status(
                        order,
                        OrderStatus.REJECTED,
                        error_message=str(e)
                    )

        return None

    async def _wait_for_order_fill(self, order: Order, timeout: int = 30) -> Optional[Order]:
        start_time = datetime.utcnow()

        while (datetime.utcnow() - start_time).seconds < timeout:
            if order.is_filled():
                logger.info(f"Order {order.id} is filled")
                return order

            if order.status in [OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED]:
                logger.warning(f"Order {order.id} reached terminal state: {order.status.value}")
                return order

            await asyncio.sleep(1)

            try:
                open_orders = await self.exchange.get_open_orders(order.symbol)
                logger.debug(f"Checking order status, open orders count: {len(open_orders)}")

                order_found = False
                for ex_order in open_orders:
                    if ex_order.get('orderId') == order.exchange_order_id:
                        order_found = True
                        filled_qty = float(ex_order.get('executedQty', 0))
                        avg_price = float(ex_order.get('avgPrice', 0))

                        logger.debug(f"Order {order.id} found: filled_qty={filled_qty}, avg_price={avg_price}")

                        if filled_qty > 0:
                            await self.order_manager.update_order_status(
                                order,
                                OrderStatus.PARTIALLY_FILLED if filled_qty < order.quantity else OrderStatus.FILLED,
                                filled_quantity=filled_qty,
                                average_price=avg_price
                            )

                        if filled_qty >= order.quantity:
                            return order

                if not order_found:
                    logger.debug(f"Order {order.exchange_order_id} not found in open orders, might be already filled")
                    # Якщо ордер не знайдено в open orders, можливо він вже виконаний
                    # Спробуємо отримати інформацію з історії ордерів
                    try:
                        order_info = await self.exchange.get_order(order.symbol, order.exchange_order_id)
                        if order_info:
                            filled_qty = float(order_info.get('executedQty', 0))
                            avg_price = float(order_info.get('avgPrice', 0))
                            status = order_info.get('status', '')

                            logger.info(f"Order from history: status={status}, filled_qty={filled_qty}, avg_price={avg_price}")

                            if status == 'FILLED' and filled_qty >= order.quantity and avg_price > 0:
                                await self.order_manager.update_order_status(
                                    order,
                                    OrderStatus.FILLED,
                                    filled_quantity=filled_qty,
                                    average_price=avg_price
                                )
                                return order
                    except Exception as e:
                        logger.debug(f"Could not get order from history: {e}")

            except Exception as e:
                logger.error(f"Error checking order status: {e}", exc_info=True)

        logger.warning(f"Order fill timeout: {order.id}, final status: {order.status.value}")
        return order

    async def _create_stop_loss_with_retry(self, position: Position, stop_loss_price: float) -> bool:
        for attempt in range(self.max_order_retries):
            try:
                close_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY

                order = Order(
                    symbol=position.symbol,
                    side=close_side,
                    order_type=OrderType.STOP_MARKET,
                    quantity=position.quantity,
                    stop_price=stop_loss_price
                )

                order = await self.order_manager.create_order(order)

                exchange_order = await self.exchange.create_order(
                    symbol=order.symbol,
                    side=order.side.value,
                    order_type=order.order_type.value,
                    quantity=order.quantity,
                    stop_price=order.stop_price,
                    reduce_only=True,
                    position_side=position.side.value
                )

                await self.order_manager.update_order_status(
                    order,
                    OrderStatus.ACCEPTED,
                    exchange_order_id=exchange_order.get('orderId')
                )

                await self.position_manager.set_stop_loss(position, stop_loss_price)

                logger.info(f"Stop loss created: {position.symbol} @ {stop_loss_price}")

                if self.event_bus:
                    await self.event_bus.publish(Event(
                        type=EventType.STOP_LOSS_CREATED,
                        data={'position_id': position.id, 'stop_loss_price': stop_loss_price},
                        source="ExecutionEngine"
                    ))

                return True

            except Exception as e:
                logger.warning(f"Stop loss creation attempt {attempt + 1} failed: {e}")

                if attempt < self.max_order_retries - 1:
                    await asyncio.sleep(self.order_retry_delay)
                else:
                    logger.error(f"Stop loss creation failed after {self.max_order_retries} attempts")

                    if self.event_bus:
                        await self.event_bus.publish(Event(
                            type=EventType.STOP_LOSS_FAILED,
                            data={'position_id': position.id, 'error': str(e)},
                            source="ExecutionEngine"
                        ))

        return False

    async def _create_take_profit_orders(self, position: Position, tp_levels: list) -> None:
        for i, tp_level in enumerate(tp_levels):
            try:
                tp_price = tp_level['price']
                tp_quantity = position.quantity * (tp_level['close_percent'] / 100)

                close_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY

                order = Order(
                    symbol=position.symbol,
                    side=close_side,
                    order_type=OrderType.TAKE_PROFIT_MARKET,
                    quantity=tp_quantity,
                    stop_price=tp_price
                )

                order = await self.order_manager.create_order(order)

                exchange_order = await self.exchange.create_order(
                    symbol=order.symbol,
                    side=order.side.value,
                    order_type=order.order_type.value,
                    quantity=order.quantity,
                    stop_price=order.stop_price,
                    reduce_only=True,
                    position_side=position.side.value
                )

                await self.order_manager.update_order_status(
                    order,
                    OrderStatus.ACCEPTED,
                    exchange_order_id=exchange_order.get('orderId')
                )

                logger.info(f"Take profit {i+1} created: {position.symbol} @ {tp_price}")

            except Exception as e:
                logger.error(f"Failed to create take profit {i+1}: {e}")

        await self.position_manager.set_take_profit_levels(position, tp_levels)

        if self.event_bus:
            await self.event_bus.publish(Event(
                type=EventType.TAKE_PROFIT_CREATED,
                data={'position_id': position.id, 'levels': len(tp_levels)},
                source="ExecutionEngine"
            ))


    async def _handle_exchange_order_update(self, event: Event) -> None:
        order_data = event.data.get('o', {})
        exchange_order_id = order_data.get('i')
        status = order_data.get('X')  # NEW, FILLED, CANCELED, etc.
        symbol = order_data.get('s')
        position_side = order_data.get('ps')  # LONG / SHORT
        realized_pnl = float(order_data.get('rp', 0))
        avg_price = float(order_data.get('ap', 0))
        order_type = order_data.get('o')  # MARKET, STOP_MARKET, TAKE_PROFIT_MARKET

        if status == 'FILLED' and order_type in ('STOP_MARKET', 'TAKE_PROFIT_MARKET', 'MARKET') and order_data.get('ro') == True:
            pos_side_enum = PositionSide.LONG if position_side == 'LONG' else PositionSide.SHORT
            position = self.position_manager.get_position(symbol, pos_side_enum)
            if position:
                await self.position_manager.close_position(position, avg_price, realized_pnl)
                logger.info(f"Position closed via exchange event: {symbol} {position_side}")

        elif status == 'CANCELED':
            local_order = await self.order_manager.get_order_by_exchange_id(str(exchange_order_id))
            if local_order:
                await self.order_manager.update_order_status(local_order, OrderStatus.CANCELLED)