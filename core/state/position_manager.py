import asyncio
import logging
from typing import Dict, List, Optional
from datetime import datetime
import json

from .position import Position, PositionSide, PositionStatus
from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, db: Database, event_bus: EventBus):
        self.db = db
        self.event_bus = event_bus
        self.positions: Dict[str, Position] = {}
        logger.info("PositionManager initialized")

    async def open_position(self, position: Position) -> Position:
        logger.info(f"Opening position: TEST")
        position.status = PositionStatus.OPEN
        position.opened_at = datetime.utcnow()

        

        tp_levels_json = json.dumps(position.take_profit_levels) if position.take_profit_levels else None
        metadata_json = json.dumps(position.metadata) if position.metadata else None

        position_id = self.db.insert_position({
            **position.to_dict(),
            'take_profit_levels': tp_levels_json,
            'metadata': metadata_json
        })
        position.id = position_id

        key = f"{position.symbol}_{position.side.value}"
        self.positions[key] = position

        logger.info(f"Position opened: {position.symbol} {position.side.value} {position.quantity} @ {position.entry_price}")

        logger.info(f"Publishing POSITION_OPENED event for {position.symbol}")
        await self.event_bus.publish(Event(
            type=EventType.POSITION_OPENED,
            data=position.to_dict(),
            source="PositionManager"
        ))
        logger.info(f"POSITION_OPENED event published for {position.symbol}")

        return position

    async def close_position(self, position: Position, close_price: float, realized_pnl: float) -> None:
        position.status = PositionStatus.CLOSED
        position.closed_at = datetime.utcnow()
        position.realized_pnl = realized_pnl

        if position.id:
            self.db.update_position(position.id, {
                'status': PositionStatus.CLOSED.value,
                'closed_at': position.closed_at,
                'realized_pnl': realized_pnl
            })

        key = f"{position.symbol}_{position.side.value}"
        if key in self.positions:
            del self.positions[key]

        logger.info(f"Position closed: {position.symbol} {position.side.value} PnL: {realized_pnl:.2f}")

        await self.event_bus.publish(Event(
            type=EventType.POSITION_CLOSED,
            data={
                **position.to_dict(),
                'close_price': close_price,
                'realized_pnl': realized_pnl
            },
            source="PositionManager"
        ))

    async def update_position(self, position: Position, current_price: float) -> None:
        position.calculate_pnl(current_price)
        position.calculate_roi(current_price)

        if position.id:
            self.db.update_position(position.id, {
                'unrealized_pnl': position.unrealized_pnl,
                'roi': position.roi
            })

        await self.event_bus.publish(Event(
            type=EventType.POSITION_UPDATED,
            data={
                **position.to_dict(),
                'current_price': current_price
            },
            source="PositionManager"
        ))

    def get_position(self, symbol: str, side: PositionSide) -> Optional[Position]:
        key = f"{symbol}_{side.value}"
        return self.positions.get(key)

    def get_all_positions(self) -> List[Position]:
        return list(self.positions.values())

    def get_open_positions(self) -> List[Position]:
        return [pos for pos in self.positions.values() if pos.is_open()]

    def get_positions_by_symbol(self, symbol: str) -> List[Position]:
        return [pos for pos in self.positions.values() if pos.symbol == symbol]

    def has_position(self, symbol: str, side: PositionSide) -> bool:
        key = f"{symbol}_{side.value}"
        return key in self.positions

    async def set_stop_loss(self, position: Position, stop_loss_price: float) -> None:
        position.stop_loss_price = stop_loss_price

        if position.id:
            self.db.update_position(position.id, {
                'stop_loss_price': stop_loss_price
            })

        logger.info(f"Stop loss set: {position.symbol} @ {stop_loss_price}")

    async def set_take_profit_levels(self, position: Position, tp_levels: List[Dict]) -> None:
        position.take_profit_levels = tp_levels

        if position.id:
            self.db.update_position(position.id, {
                'take_profit_levels': json.dumps(tp_levels)
            })

        logger.info(f"Take profit levels set: {position.symbol} {len(tp_levels)} levels")

    async def recover_positions(self) -> None:
        logger.info("Recovering positions from database")
        db_positions = self.db.get_open_positions()

        for db_pos in db_positions:
            tp_levels = json.loads(db_pos['take_profit_levels']) if db_pos['take_profit_levels'] else None
            metadata = json.loads(db_pos['metadata']) if db_pos['metadata'] else None

            position = Position(
                id=db_pos['id'],
                symbol=db_pos['symbol'],
                side=PositionSide(db_pos['side']),
                entry_price=db_pos['entry_price'],
                quantity=db_pos['quantity'],
                leverage=db_pos['leverage'],
                margin=db_pos['margin'],
                liquidation_price=db_pos['liquidation_price'],
                unrealized_pnl=db_pos['unrealized_pnl'],
                realized_pnl=db_pos['realized_pnl'],
                roi=db_pos['roi'],
                status=PositionStatus(db_pos['status']),
                opened_at=datetime.fromisoformat(db_pos['opened_at']),
                stop_loss_price=db_pos['stop_loss_price'],
                take_profit_levels=tp_levels,
                metadata=metadata
            )

            key = f"{position.symbol}_{position.side.value}"
            self.positions[key] = position
            logger.info(f"Recovered position: {position.symbol} {position.side.value}")

        logger.info(f"Recovered {len(db_positions)} positions")

    def get_position_count(self) -> int:
        return len(self.positions)

    def get_total_margin_used(self) -> float:
        return sum(pos.margin for pos in self.positions.values())

    def get_total_unrealized_pnl(self) -> float:
        return sum(pos.unrealized_pnl for pos in self.positions.values())
