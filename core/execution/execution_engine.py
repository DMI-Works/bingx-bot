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
        db: Database
    ):
        self.exchange = exchange
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.event_bus = event_bus
        self.db = db

        self.max_order_retries = 3
        self.order_retry_delay = 2

        self.event_bus.subscribe(EventType.SIGNAL_GENERATED, self._handle_signal)
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

            await self.order_manager.update_order_status(
                order,
                OrderStatus.SENT,
                exchange_order_id=exchange_order.get('orderId')
            )

            filled_order = await self._wait_for_order_fill(order)

            if not filled_order or not filled_order.is_filled():
                logger.error(f"Order not filled: {order.id}")
                return None

            entry_price = filled_order.average_price
            margin = (entry_price * quantity) / leverage

            position = Position(
                symbol=symbol,
                side=PositionSide.LONG if side == 'LONG' else PositionSide.SHORT,
                entry_price=entry_price,
                quantity=quantity,
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

            exchange_order = await self._send_order_with_retry(order, reduce_only=True)

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

    async def _send_order_with_retry(self, order: Order, reduce_only: bool = False) -> Optional[dict]:
        for attempt in range(self.max_order_retries):
            try:
                exchange_order = await self.exchange.create_order(
                    symbol=order.symbol,
                    side=order.side.value,
                    order_type=order.order_type.value,
                    quantity=order.quantity,
                    price=order.price,
                    stop_price=order.stop_price,
                    reduce_only=reduce_only
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
                return order

            if order.status in [OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED]:
                logger.warning(f"Order {order.id} reached terminal state: {order.status.value}")
                return order

            await asyncio.sleep(1)

            try:
                open_orders = await self.exchange.get_open_orders(order.symbol)

                for ex_order in open_orders:
                    if ex_order.get('orderId') == order.exchange_order_id:
                        filled_qty = float(ex_order.get('executedQty', 0))
                        avg_price = float(ex_order.get('avgPrice', 0))

                        if filled_qty > 0:
                            await self.order_manager.update_order_status(
                                order,
                                OrderStatus.PARTIALLY_FILLED if filled_qty < order.quantity else OrderStatus.FILLED,
                                filled_quantity=filled_qty,
                                average_price=avg_price
                            )

                        if filled_qty >= order.quantity:
                            return order

            except Exception as e:
                logger.error(f"Error checking order status: {e}")

        logger.warning(f"Order fill timeout: {order.id}")
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
                    reduce_only=True
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
                    reduce_only=True
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
