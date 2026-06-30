from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


class OrderStatus(Enum):
    CREATED = "CREATED"
    SENT = "SENT"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"


@dataclass
class Order:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    stop_price: Optional[float] = None

    id: Optional[int] = None
    exchange_order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: float = 0.0
    average_price: Optional[float] = None
    commission: float = 0.0
    commission_asset: Optional[str] = None
    created_at: datetime = None
    updated_at: datetime = None
    error_message: Optional[str] = None
    metadata: Optional[dict] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.utcnow()
        if self.updated_at is None:
            self.updated_at = datetime.utcnow()

    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    def is_open(self) -> bool:
        return self.status in [
            OrderStatus.CREATED,
            OrderStatus.SENT,
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED
        ]

    def is_closed(self) -> bool:
        return self.status in [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED
        ]

    def fill_percentage(self) -> float:
        if self.quantity == 0:
            return 0.0
        return (self.filled_quantity / self.quantity) * 100

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'exchange_order_id': self.exchange_order_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'quantity': self.quantity,
            'price': self.price,
            'stop_price': self.stop_price,
            'status': self.status.value,
            'filled_quantity': self.filled_quantity,
            'average_price': self.average_price,
            'commission': self.commission,
            'commission_asset': self.commission_asset,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'error_message': self.error_message,
            'metadata': self.metadata
        }
