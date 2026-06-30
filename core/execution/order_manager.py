import asyncio
import logging
from typing import Dict, Optional
from datetime import datetime

from .order import Order, OrderStatus
from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, db: Database, event_bus: EventBus):
        self.db = db
        self.event_bus = event_bus
        self.orders: Dict[str, Order] = {}
        logger.info("OrderManager initialized")

    async def create_order(self, order: Order) -> Order:
        order.status = OrderStatus.CREATED
        order.created_at = datetime.utcnow()
        order.updated_at = datetime.utcnow()

        order_id = self.db.insert_order(order.to_dict())
        order.id = order_id

        self.orders[str(order_id)] = order
        logger.info(f"Order created: {order.symbol} {order.side.value} {order.quantity} @ {order.price}")

        await self.event_bus.publish(Event(
            type=EventType.ORDER_CREATED,
            data=order.to_dict(),
            source="OrderManager"
        ))

        return order

    async def update_order_status(self, order: Order, new_status: OrderStatus, **kwargs) -> None:
        old_status = order.status
        order.status = new_status
        order.updated_at = datetime.utcnow()

        for key, value in kwargs.items():
            if hasattr(order, key):
                setattr(order, key, value)

        updates = {'status': new_status.value, 'updated_at': order.updated_at}
        updates.update({k: v for k, v in kwargs.items() if k in [
            'exchange_order_id', 'filled_quantity', 'average_price',
            'commission', 'commission_asset', 'error_message'
        ]})

        if order.id:
            self.db.update_order(order.id, updates)

        logger.info(f"Order status changed: {order.symbol} {old_status.value} -> {new_status.value}")

        event_type_map = {
            OrderStatus.SENT: EventType.ORDER_SENT,
            OrderStatus.ACCEPTED: EventType.ORDER_ACCEPTED,
            OrderStatus.PARTIALLY_FILLED: EventType.ORDER_PARTIALLY_FILLED,
            OrderStatus.FILLED: EventType.ORDER_FILLED,
            OrderStatus.CANCELLED: EventType.ORDER_CANCELLED,
            OrderStatus.REJECTED: EventType.ORDER_REJECTED,
            OrderStatus.EXPIRED: EventType.ORDER_EXPIRED
        }

        if new_status in event_type_map:
            await self.event_bus.publish(Event(
                type=event_type_map[new_status],
                data=order.to_dict(),
                source="OrderManager"
            ))

    async def get_order(self, order_id: str) -> Optional[Order]:
        return self.orders.get(order_id)

    async def get_order_by_exchange_id(self, exchange_order_id: str) -> Optional[Order]:
        for order in self.orders.values():
            if order.exchange_order_id == exchange_order_id:
                return order
        return None

    def get_open_orders(self) -> list[Order]:
        return [order for order in self.orders.values() if order.is_open()]

    def get_orders_by_symbol(self, symbol: str) -> list[Order]:
        return [order for order in self.orders.values() if order.symbol == symbol]

    async def cancel_order(self, order: Order) -> None:
        await self.update_order_status(order, OrderStatus.CANCELLED)

    async def recover_orders(self) -> None:
        logger.info("Recovering orders from database")
        db_orders = self.db.get_open_orders()

        for db_order in db_orders:
            from .order import OrderSide, OrderType

            order = Order(
                id=db_order['id'],
                exchange_order_id=db_order['exchange_order_id'],
                symbol=db_order['symbol'],
                side=OrderSide(db_order['side']),
                order_type=OrderType(db_order['order_type']),
                quantity=db_order['quantity'],
                price=db_order['price'],
                stop_price=db_order['stop_price'],
                status=OrderStatus(db_order['status']),
                filled_quantity=db_order['filled_quantity'],
                average_price=db_order['average_price'],
                commission=db_order['commission'],
                commission_asset=db_order['commission_asset'],
                created_at=datetime.fromisoformat(db_order['created_at']),
                updated_at=datetime.fromisoformat(db_order['updated_at']),
                error_message=db_order['error_message']
            )

            self.orders[str(order.id)] = order
            logger.info(f"Recovered order: {order.id} {order.symbol} {order.status.value}")

        logger.info(f"Recovered {len(db_orders)} orders")

    def get_order_count(self) -> int:
        return len(self.orders)

    def get_open_order_count(self) -> int:
        return len(self.get_open_orders())
