import logging
from typing import Dict, List, Optional
from datetime import datetime

from ..exchange import BingXClient
from ..execution import OrderManager
from ..state import PositionManager
from ..database import Database
from ..events import EventBus, Event, EventType


logger = logging.getLogger(__name__)


class RecoveryEngine:
    def __init__(
        self,
        exchange: BingXClient,
        order_manager: OrderManager,
        position_manager: PositionManager,
        db: Database,
        event_bus: EventBus
    ):
        self.exchange = exchange
        self.order_manager = order_manager
        self.position_manager = position_manager
        self.db = db
        self.event_bus = event_bus

        logger.info("RecoveryEngine initialized")

    async def recover(self) -> bool:
        try:
            logger.info("Starting recovery process...")

            await self.event_bus.publish(Event(
                type=EventType.RECOVERY_STARTED,
                data={'timestamp': datetime.utcnow().isoformat()},
                source="RecoveryEngine"
            ))

            await self.order_manager.recover_orders()

            await self.position_manager.recover_positions()

            exchange_positions = await self.exchange.get_positions()
            await self._sync_positions(exchange_positions)

            exchange_orders = await self.exchange.get_open_orders()
            await self._sync_orders(exchange_orders)

            balance = await self.exchange.get_account_balance()
            await self._sync_balance(balance)

            await self._verify_stop_loss_orders()

            logger.info("Recovery completed successfully")

            await self.event_bus.publish(Event(
                type=EventType.RECOVERY_COMPLETED,
                data={
                    'timestamp': datetime.utcnow().isoformat(),
                    'positions_recovered': self.position_manager.get_position_count(),
                    'orders_recovered': self.order_manager.get_order_count()
                },
                source="RecoveryEngine"
            ))

            return True

        except Exception as e:
            logger.error(f"Recovery failed: {e}", exc_info=True)

            await self.event_bus.publish(Event(
                type=EventType.RECOVERY_FAILED,
                data={'error': str(e)},
                source="RecoveryEngine"
            ))

            return False

    async def _sync_positions(self, exchange_positions: List[Dict]) -> None:
        logger.info(f"Syncing {len(exchange_positions)} positions from exchange")

        local_positions = self.position_manager.get_all_positions()

        for ex_pos in exchange_positions:
            symbol = ex_pos.get('symbol')
            position_side = ex_pos.get('positionSide', 'LONG')

            quantity = float(ex_pos.get('positionAmt', 0))
            if quantity == 0:
                continue

            from ..state import PositionSide
            side = PositionSide.LONG if position_side == 'LONG' else PositionSide.SHORT

            local_pos = self.position_manager.get_position(symbol, side)

            if not local_pos:
                logger.warning(f"Position found on exchange but not in DB: {symbol} {side.value}")

                from ..state import Position
                position = Position(
                    symbol=symbol,
                    side=side,
                    entry_price=float(ex_pos.get('entryPrice', 0)),
                    quantity=abs(quantity),
                    leverage=int(ex_pos.get('leverage', 10)),
                    margin=float(ex_pos.get('isolatedWallet', 0)),
                    unrealized_pnl=float(ex_pos.get('unRealizedProfit', 0))
                )

                await self.position_manager.open_position(position)
                logger.info(f"Position recovered and added to DB: {symbol} {side.value}")

    async def _sync_orders(self, exchange_orders: List[Dict]) -> None:
        logger.info(f"Syncing {len(exchange_orders)} orders from exchange")

        for ex_order in exchange_orders:
            order_id = ex_order.get('orderId')

            local_order = await self.order_manager.get_order_by_exchange_id(str(order_id))

            if not local_order:
                logger.warning(f"Order found on exchange but not in DB: {order_id}")

    async def _sync_balance(self, balance_data: Dict) -> None:
        logger.info("Syncing balance from exchange")

        if 'data' in balance_data:
            balance = balance_data['data'].get('balance', {})
            asset = balance.get('asset', 'USDT')
            free = float(balance.get('availableMargin', 0))
            locked = float(balance.get('usedMargin', 0))

            self.db.insert_balance(asset, free, locked)
            logger.info(f"Balance synced: {asset} {free + locked}")

            await self.event_bus.publish(Event(
                type=EventType.BALANCE_UPDATED,
                data={'asset': asset, 'free': free, 'locked': locked},
                source="RecoveryEngine"
            ))

    async def _verify_stop_loss_orders(self) -> None:
        logger.info("Verifying stop loss orders for all positions")

        positions = self.position_manager.get_open_positions()

        for position in positions:
            if not position.stop_loss_price:
                logger.warning(f"Position without stop loss: {position.symbol} {position.side.value}")
                continue

            open_orders = await self.exchange.get_open_orders(position.symbol)

            has_stop_loss = False
            for order in open_orders:
                if order.get('type') == 'STOP_MARKET' and order.get('reduceOnly'):
                    has_stop_loss = True
                    break

            if not has_stop_loss:
                logger.error(f"CRITICAL: Position missing stop loss order: {position.symbol} {position.side.value}")

                await self.event_bus.publish(Event(
                    type=EventType.STOP_LOSS_FAILED,
                    data={'position_id': position.id, 'symbol': position.symbol},
                    source="RecoveryEngine"
                ))
