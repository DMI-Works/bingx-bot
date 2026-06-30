from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class PositionStatus(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class Position:
    symbol: str
    side: PositionSide
    entry_price: float
    quantity: float
    leverage: int
    margin: float

    id: Optional[int] = None
    liquidation_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    roi: float = 0.0
    status: PositionStatus = PositionStatus.OPEN
    opened_at: datetime = None
    closed_at: Optional[datetime] = None
    stop_loss_price: Optional[float] = None
    take_profit_levels: Optional[List[Dict]] = None
    metadata: Optional[dict] = None

    def __post_init__(self):
        if self.opened_at is None:
            self.opened_at = datetime.utcnow()

    def calculate_pnl(self, current_price: float) -> float:
        if self.side == PositionSide.LONG:
            pnl = (current_price - self.entry_price) * self.quantity
        else:
            pnl = (self.entry_price - current_price) * self.quantity

        self.unrealized_pnl = pnl
        return pnl

    def calculate_roi(self, current_price: float) -> float:
        pnl = self.calculate_pnl(current_price)
        if self.margin == 0:
            return 0.0

        roi = (pnl / self.margin) * 100
        self.roi = roi
        return roi

    def is_open(self) -> bool:
        return self.status == PositionStatus.OPEN

    def is_closed(self) -> bool:
        return self.status == PositionStatus.CLOSED

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'symbol': self.symbol,
            'side': self.side.value,
            'entry_price': self.entry_price,
            'quantity': self.quantity,
            'leverage': self.leverage,
            'margin': self.margin,
            'liquidation_price': self.liquidation_price,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
            'roi': self.roi,
            'status': self.status.value,
            'opened_at': self.opened_at.isoformat() if self.opened_at else None,
            'closed_at': self.closed_at.isoformat() if self.closed_at else None,
            'stop_loss_price': self.stop_loss_price,
            'take_profit_levels': self.take_profit_levels,
            'metadata': self.metadata
        }
